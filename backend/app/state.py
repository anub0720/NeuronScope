"""Process-local, bounded storage for live experiment data."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.config import settings


class StateError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass
class Subscriber:
    subscriber_id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]


@dataclass
class StagedEpoch:
    chunk_count: int
    chunks: dict[int, dict[str, Any]] = field(default_factory=dict)
    chunk_digests: dict[int, str] = field(default_factory=dict)
    size_bytes: int = 0


@dataclass
class ExperimentState:
    experiment_id: str
    project_id: str
    name: str
    framework: str
    model_name: str | None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_activity: float = field(default_factory=time.monotonic)
    last_activity_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    current_epoch: int = -1
    revision: int = 0
    finished: bool = False
    architecture: dict[str, Any] | None = None
    classes: dict[str, str] = field(default_factory=dict)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    neuron_layers: dict[str, str] = field(default_factory=dict)
    live_neuron_buffers: dict[str, deque[dict[str, Any]]] = field(
        default_factory=dict
    )
    live_row_sizes: dict[str, deque[int]] = field(default_factory=dict)
    architecture_memory_bytes: int = 0
    live_memory_bytes: int = 0
    event_memory_bytes: int = 0
    event_sizes: dict[str, int] = field(default_factory=dict)
    epochs: deque[int] = field(
        default_factory=lambda: deque(maxlen=settings.METRIC_WINDOW_SIZE)
    )
    recent_epoch_digests: deque[tuple[int, str]] = field(
        default_factory=lambda: deque(maxlen=settings.METRIC_WINDOW_SIZE)
    )
    staged_epochs: dict[int, StagedEpoch] = field(default_factory=dict)
    completed_chunk_epochs: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=settings.METRIC_WINDOW_SIZE)
    )
    collapse_events: dict[str, dict[str, Any]] = field(default_factory=dict)
    event_requests: dict[str, dict[str, str]] = field(default_factory=dict)
    unresolved_events: dict[tuple[str, str, tuple[int, ...]], str] = field(
        default_factory=dict
    )


class LiveState:
    """Concurrency-safe, process-local state manager."""

    def __init__(self) -> None:
        self.active_experiments: dict[str, ExperimentState] = {}
        self._subscribers: dict[str, dict[str, Subscriber]] = {}
        self._lock = threading.RLock()

    def create_experiment(
        self,
        experiment_id: str,
        project_id: str,
        name: str,
        framework: str,
        model_name: str | None,
    ) -> ExperimentState:
        with self._lock:
            if experiment_id in self.active_experiments:
                raise StateError(409, "Experiment is already active")
            if len(self.active_experiments) >= settings.MAX_ACTIVE_EXPERIMENTS:
                raise StateError(429, "Maximum active experiment capacity reached")
            state = ExperimentState(
                experiment_id=experiment_id,
                project_id=project_id,
                name=name,
                framework=framework,
                model_name=model_name,
            )
            self.active_experiments[experiment_id] = state
            return state

    def get(self, experiment_id: str, project_id: str) -> ExperimentState:
        with self._lock:
            state = self.active_experiments.get(experiment_id)
            if state is None or state.project_id != project_id:
                raise StateError(404, "Active experiment not found")
            self._touch(state)
            return state

    def discard_uncommitted_experiment(self, experiment_id: str) -> None:
        with self._lock:
            self.active_experiments.pop(experiment_id, None)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self.active_experiments)

    def register_architecture(
        self,
        experiment_id: str,
        project_id: str,
        architecture: dict[str, Any],
    ) -> bool:
        with self._lock:
            state = self.get(experiment_id, project_id)
            if state.finished:
                raise StateError(409, "Finished experiments cannot be modified")
            neuron_total = sum(layer["neuron_count"] for layer in architecture["layers"])
            if neuron_total > settings.MAX_NEURONS_PER_EXPERIMENT:
                raise StateError(413, "Architecture exceeds the neuron limit")
            normalized = copy.deepcopy(architecture)
            if state.architecture is not None:
                if state.architecture == normalized:
                    return True
                raise StateError(409, "Experiment architecture is already registered")
            architecture_size = self._json_size(normalized)
            if architecture_size > settings.MAX_EXPERIMENT_MEMORY_BYTES:
                raise StateError(413, "Architecture exceeds the experiment memory limit")
            state.architecture = normalized
            state.architecture_memory_bytes = architecture_size
            state.framework = normalized["framework"]
            state.model_name = normalized["model_name"]
            state.classes = dict(normalized["classes"])
            state.layers = {layer["layer_id"]: layer for layer in normalized["layers"]}
            for layer in normalized["layers"]:
                for neuron_id in layer.get("neuron_ids") or []:
                    state.neuron_layers[neuron_id] = layer["layer_id"]
            return False

    def ingest_epoch(
        self,
        experiment_id: str,
        project_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        epoch = message["epoch"]
        canonical = json.dumps(
            message, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        with self._lock:
            state = self.get(experiment_id, project_id)
            if state.finished:
                raise StateError(409, "Finished experiments do not accept epochs")
            if state.architecture is None:
                raise StateError(409, "Register the architecture before sending epochs")
            if state.staged_epochs:
                raise StateError(
                    409,
                    "Complete the staged epoch before accepting another epoch",
                )

            known_digest = dict(state.recent_epoch_digests).get(epoch)
            if known_digest is not None:
                if known_digest == digest:
                    return {
                        "status": "already_received",
                        "duplicate": True,
                        "epoch": epoch,
                    }
                raise StateError(409, "Epoch was already received with different data")
            if epoch <= state.current_epoch:
                raise StateError(409, "Epoch numbers must increase monotonically")

            incoming = message["neurons"]
            candidate_neuron_layers = dict(state.neuron_layers)
            layer_neuron_counts: dict[str, int] = {}
            for assigned_layer in candidate_neuron_layers.values():
                layer_neuron_counts[assigned_layer] = (
                    layer_neuron_counts.get(assigned_layer, 0) + 1
                )
            for neuron_id, metrics in incoming.items():
                layer_id = metrics["layer_id"]
                layer = state.layers.get(layer_id)
                if layer is None:
                    raise StateError(422, f"Unknown layer ID: {layer_id}")
                declared_ids = layer.get("neuron_ids")
                if declared_ids is not None and neuron_id not in declared_ids:
                    raise StateError(422, f"Unknown neuron ID: {neuron_id}")
                mapped_layer = candidate_neuron_layers.get(neuron_id)
                if mapped_layer is not None and mapped_layer != layer_id:
                    raise StateError(422, f"Neuron {neuron_id} changed layers")
                if mapped_layer is None:
                    if layer_neuron_counts.get(layer_id, 0) >= layer["neuron_count"]:
                        raise StateError(422, f"Layer {layer_id} exceeds neuron_count")
                    candidate_neuron_layers[neuron_id] = layer_id
                    layer_neuron_counts[layer_id] = (
                        layer_neuron_counts.get(layer_id, 0) + 1
                    )
                for field_name in (
                    "class_contribution",
                    "class_sample_counts",
                    "class_validity_mask",
                ):
                    values = metrics.get(field_name)
                    if values is not None and len(values) != len(state.classes):
                        raise StateError(
                            422,
                            f"{field_name} length must match the registered class mapping",
                        )

            if len(candidate_neuron_layers) > settings.MAX_NEURONS_PER_EXPERIMENT:
                raise StateError(413, "Experiment exceeds the neuron limit")

            previous_epochs = list(state.epochs)
            projected_live_bytes = state.live_memory_bytes
            proposed_row_sizes: dict[str, list[int]] = {}
            for neuron_id in candidate_neuron_layers:
                sizes = state.live_row_sizes.get(neuron_id)
                if sizes is None:
                    sizes = deque(maxlen=settings.METRIC_WINDOW_SIZE)
                    prior_window = max(settings.METRIC_WINDOW_SIZE - 1, 0)
                    for previous_epoch in (
                        previous_epochs[-prior_window:] if prior_window else []
                    ):
                        sizes.append(
                            self._json_size(
                                {"epoch": previous_epoch, "metrics": None}
                            )
                        )
                else:
                    sizes = deque(sizes, maxlen=settings.METRIC_WINDOW_SIZE)
                if len(sizes) == settings.METRIC_WINDOW_SIZE:
                    projected_live_bytes -= sizes[0]
                row_size = self._json_size(
                    {
                        "epoch": epoch,
                        "metrics": incoming.get(neuron_id),
                    }
                )
                sizes.append(row_size)
                projected_live_bytes += row_size
                proposed_row_sizes[neuron_id] = list(sizes)
            if (
                state.architecture_memory_bytes
                + projected_live_bytes
                + state.event_memory_bytes
                > settings.MAX_EXPERIMENT_MEMORY_BYTES
            ):
                raise StateError(429, "Experiment memory capacity reached")

            state.neuron_layers = candidate_neuron_layers
            for neuron_id in state.neuron_layers:
                buffer = state.live_neuron_buffers.get(neuron_id)
                if buffer is None:
                    buffer = deque(maxlen=settings.METRIC_WINDOW_SIZE)
                    prior_window = max(settings.METRIC_WINDOW_SIZE - 1, 0)
                    for previous_epoch in (
                        previous_epochs[-prior_window:] if prior_window else []
                    ):
                        buffer.append({"epoch": previous_epoch, "metrics": None})
                    state.live_neuron_buffers[neuron_id] = buffer
                    state.live_row_sizes[neuron_id] = deque(
                        proposed_row_sizes[neuron_id][:-1],
                        maxlen=settings.METRIC_WINDOW_SIZE,
                    )
                buffer.append(
                    {
                        "epoch": epoch,
                        "metrics": copy.deepcopy(incoming.get(neuron_id)),
                    }
                )
                state.live_row_sizes[neuron_id].append(
                    proposed_row_sizes[neuron_id][-1]
                )

            state.epochs.append(epoch)
            state.recent_epoch_digests.append((epoch, digest))
            state.current_epoch = epoch
            state.live_memory_bytes = projected_live_bytes
            self._touch(state)

            update = {
                "type": "epoch_update",
                "experiment_id": experiment_id,
                "epoch": epoch,
                "neurons": {
                    neuron_id: {
                        "layer_id": state.neuron_layers[neuron_id],
                        "metrics": copy.deepcopy(metrics),
                    }
                    for neuron_id, metrics in incoming.items()
                },
            }
            self._publish_locked(experiment_id, update)
            return {"status": "accepted", "duplicate": False, "epoch": epoch}

    def stage_epoch_chunk(
        self,
        experiment_id: str,
        project_id: str,
        epoch: int,
        chunk_index: int,
        chunk_count: int,
        neurons: dict[str, Any],
    ) -> dict[str, Any]:
        chunk_digest = hashlib.sha256(
            json.dumps(
                {"neurons": neurons},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        chunk_size = self._json_size(neurons)

        with self._lock:
            state = self.get(experiment_id, project_id)
            if state.finished:
                raise StateError(409, "Finished experiments do not accept epochs")
            if state.architecture is None:
                raise StateError(409, "Register the architecture before sending epochs")
            if chunk_count < 1 or chunk_count > 10000:
                raise StateError(422, "chunk_count must be between 1 and 10000")
            if chunk_index < 0 or chunk_index >= chunk_count:
                raise StateError(422, "chunk_index must be within chunk_count")
            if not neurons:
                raise StateError(422, "Epoch chunks must contain at least one neuron")

            completed = next(
                (
                    entry
                    for entry in state.completed_chunk_epochs
                    if entry["epoch"] == epoch
                ),
                None,
            )
            if completed is not None:
                previous_digest = completed["chunk_digests"].get(chunk_index)
                if (
                    completed["chunk_count"] == chunk_count
                    and previous_digest == chunk_digest
                ):
                    return {
                        "status": "already_received",
                        "duplicate": True,
                        "epoch": epoch,
                        "chunk_index": chunk_index,
                    }
                raise StateError(409, "Completed epoch chunk conflicts with prior data")
            if epoch <= state.current_epoch:
                raise StateError(409, "Epoch numbers must increase monotonically")

            staged = state.staged_epochs.get(epoch)
            if staged is None:
                if state.staged_epochs:
                    raise StateError(409, "Another epoch is awaiting completion")
                staged = StagedEpoch(chunk_count=chunk_count)
                state.staged_epochs[epoch] = staged
            elif staged.chunk_count != chunk_count:
                raise StateError(409, "chunk_count changed while staging an epoch")

            previous_digest = staged.chunk_digests.get(chunk_index)
            if previous_digest is not None:
                if previous_digest == chunk_digest:
                    return {
                        "status": "already_received",
                        "duplicate": True,
                        "epoch": epoch,
                        "chunk_index": chunk_index,
                    }
                raise StateError(409, "Epoch chunk was already received with different data")

            all_neuron_ids = {
                neuron_id
                for existing_chunk in staged.chunks.values()
                for neuron_id in existing_chunk
            }
            if all_neuron_ids.intersection(neurons):
                raise StateError(409, "Neuron appears in more than one epoch chunk")

            projected_bytes = (
                state.architecture_memory_bytes
                + state.live_memory_bytes
                + state.event_memory_bytes
                + staged.size_bytes
                + chunk_size
            )
            if projected_bytes > settings.MAX_EXPERIMENT_MEMORY_BYTES:
                raise StateError(429, "Staged epoch exceeds experiment memory capacity")
            staged.chunks[chunk_index] = copy.deepcopy(neurons)
            staged.chunk_digests[chunk_index] = chunk_digest
            staged.size_bytes += chunk_size
            return {
                "status": "staged",
                "duplicate": False,
                "epoch": epoch,
                "chunk_index": chunk_index,
                "received_chunks": len(staged.chunks),
                "chunk_count": staged.chunk_count,
            }

    def complete_staged_epoch(
        self,
        experiment_id: str,
        project_id: str,
        epoch: int,
        chunk_count: int,
    ) -> dict[str, Any]:
        with self._lock:
            state = self.get(experiment_id, project_id)
            completed = next(
                (
                    entry
                    for entry in state.completed_chunk_epochs
                    if entry["epoch"] == epoch
                ),
                None,
            )
            if completed is not None:
                if completed["chunk_count"] == chunk_count:
                    return {
                        "status": "already_received",
                        "duplicate": True,
                        "epoch": epoch,
                    }
                raise StateError(409, "Completed epoch chunk_count conflicts")

            staged = state.staged_epochs.get(epoch)
            if staged is None:
                raise StateError(409, "No staged chunks exist for this epoch")
            if staged.chunk_count != chunk_count:
                raise StateError(409, "chunk_count does not match staged epoch")
            if len(staged.chunks) != staged.chunk_count:
                raise StateError(409, "Epoch is missing one or more chunks")

            neurons: dict[str, Any] = {}
            for index in range(chunk_count):
                if index not in staged.chunks:
                    raise StateError(409, "Epoch is missing one or more chunks")
                neurons.update(copy.deepcopy(staged.chunks[index]))

            del state.staged_epochs[epoch]
            try:
                result = self.ingest_epoch(
                    experiment_id,
                    project_id,
                    {"epoch": epoch, "neurons": neurons},
                )
            except Exception:
                state.staged_epochs[epoch] = staged
                raise

            state.completed_chunk_epochs.append(
                {
                    "epoch": epoch,
                    "chunk_count": chunk_count,
                    "chunk_digests": dict(staged.chunk_digests),
                }
            )
            return result

    def register_event(
        self,
        experiment_id: str,
        project_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            state = self.get(experiment_id, project_id)
            if state.finished:
                raise StateError(409, "Finished experiments cannot be modified")
            if state.architecture is None:
                raise StateError(409, "Register the architecture before sending events")

            neuron_id = event["neuron_id"]
            layer_id = event["layer_id"]
            layer = state.layers.get(layer_id)
            if layer is None:
                raise StateError(422, f"Unknown layer ID: {layer_id}")
            mapped_layer = state.neuron_layers.get(neuron_id)
            if mapped_layer is not None and mapped_layer != layer_id:
                raise StateError(422, f"Neuron {neuron_id} changed layers")
            candidate_neuron_layers = dict(state.neuron_layers)
            if mapped_layer is None and layer.get("neuron_ids") is not None:
                if neuron_id not in layer["neuron_ids"]:
                    raise StateError(422, f"Unknown neuron ID: {neuron_id}")
                candidate_neuron_layers[neuron_id] = layer_id
            elif mapped_layer is None:
                layer_neuron_count = sum(
                    assigned_layer == layer_id
                    for assigned_layer in candidate_neuron_layers.values()
                )
                if layer_neuron_count >= layer["neuron_count"]:
                    raise StateError(422, f"Layer {layer_id} exceeds neuron_count")
                candidate_neuron_layers[neuron_id] = layer_id

            classes = tuple(sorted(event["affected_classes"]))
            if any(index >= len(state.classes) for index in classes):
                raise StateError(422, "Affected class index is not registered")
            key = (neuron_id, event["event_type"], classes)
            event_id = event["event_id"]
            request_digest = hashlib.sha256(
                json.dumps(
                    event, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            ).hexdigest()
            previous_requests = state.event_requests.get(event_id, {})
            previous_event_id = previous_requests.get(request_digest)
            if previous_event_id is not None:
                previous_event = state.collapse_events.get(previous_event_id)
                if previous_event is not None:
                    return {
                        "status": "already_received",
                        "duplicate": True,
                        "event": copy.deepcopy(previous_event),
                    }
            if event["status"] == "recovered":
                previous_id = state.unresolved_events.get(key)
                if previous_id is None:
                    if previous_requests:
                        raise StateError(409, "Event ID is already in use")
                    raise StateError(409, "No unresolved matching collapse event exists")
                existing = state.collapse_events[previous_id]
                if event_id in state.collapse_events and event_id != previous_id:
                    raise StateError(409, "Event ID is already in use")
                updated_event = copy.deepcopy(existing)
                updated_event["status"] = "recovered"
                updated_event["recovery"] = copy.deepcopy(event.get("recovery"))
                recovered_epoch = (
                    event["recovery"].get("epoch")
                    if event.get("recovery")
                    else event["detected_epoch"]
                )
                if (
                    recovered_epoch is not None
                    and recovered_epoch < updated_event["detected_epoch"]
                ):
                    raise StateError(422, "Recovery cannot precede the collapse event")
                updated_event["recovered_epoch"] = recovered_epoch
                updated_event["updated_at"] = datetime.now(timezone.utc).isoformat()
                old_size = state.event_sizes[previous_id]
                new_size = self._json_size(updated_event)
                if (
                    state.architecture_memory_bytes
                    + state.live_memory_bytes
                    + state.event_memory_bytes
                    + self._staged_memory_bytes(state)
                    - old_size
                    + new_size
                    > settings.MAX_EXPERIMENT_MEMORY_BYTES
                ):
                    raise StateError(429, "Experiment memory capacity reached")
                state.collapse_events[previous_id] = updated_event
                state.event_sizes[previous_id] = new_size
                state.event_memory_bytes += new_size - old_size
                del state.unresolved_events[key]
                state.event_requests.setdefault(event_id, {})[
                    request_digest
                ] = previous_id
                self._publish_locked(
                    experiment_id,
                    {"type": "collapse_recovered", "event": copy.deepcopy(updated_event)},
                )
                return {
                    "status": "recovered",
                    "duplicate": False,
                    "event": updated_event,
                }

            if event_id in state.collapse_events:
                raise StateError(409, "Event ID is already in use")
            if previous_requests:
                raise StateError(409, "Event ID is already in use")

            previous_id = state.unresolved_events.get(key)
            if previous_id is not None:
                return {
                    "status": "already_active",
                    "duplicate": True,
                    "event": copy.deepcopy(state.collapse_events[previous_id]),
                }
            if len(state.collapse_events) >= settings.MAX_COLLAPSE_EVENTS_PER_EXPERIMENT:
                raise StateError(429, "Collapse event capacity reached; clear events to continue")

            evidence = event.get("evidence")
            if evidence is None:
                evidence = self._snapshot_neuron(state, neuron_id)
            else:
                evidence = self._validate_evidence(
                    state,
                    event_id,
                    neuron_id,
                    event["detected_epoch"],
                    evidence,
                )
            now = datetime.now(timezone.utc).isoformat()
            stored = {
                **copy.deepcopy(event),
                "experiment_id": experiment_id,
                "evidence": copy.deepcopy(evidence),
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
            event_size = self._json_size(stored)
            if (
                state.architecture_memory_bytes
                + state.live_memory_bytes
                + state.event_memory_bytes
                + self._staged_memory_bytes(state)
                + event_size
                > settings.MAX_EXPERIMENT_MEMORY_BYTES
            ):
                raise StateError(429, "Experiment memory capacity reached")
            state.neuron_layers = candidate_neuron_layers
            state.collapse_events[event_id] = stored
            state.event_sizes[event_id] = event_size
            state.event_memory_bytes += event_size
            state.event_requests.setdefault(event_id, {})[
                request_digest
            ] = event_id
            state.unresolved_events[key] = event_id
            self._publish_locked(
                experiment_id,
                {"type": "collapse_event", "event": copy.deepcopy(stored)},
            )
            return {"status": "registered", "duplicate": False, "event": stored}

    def _validate_evidence(
        self,
        state: ExperimentState,
        event_id: str,
        neuron_id: str,
        detected_epoch: int,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        epochs = evidence.get("epochs")
        if epochs is not None:
            if (
                not isinstance(epochs, list)
                or any(not isinstance(value, int) or value < 0 for value in epochs)
                or epochs != sorted(set(epochs))
                or any(value > detected_epoch for value in epochs)
                or len(epochs) > settings.METRIC_WINDOW_SIZE
            ):
                raise StateError(422, f"Invalid evidence epochs for event {event_id}")
            for field_name, values in evidence.items():
                if field_name == "epochs":
                    continue
                if not isinstance(values, list):
                    raise StateError(422, f"Evidence field {field_name} must be an array")
                if field_name not in {
                    "class_contribution",
                    "class_contribution_uncertainty",
                }:
                    if len(values) != len(epochs):
                        raise StateError(
                            422, f"Evidence field {field_name} must match epochs"
                        )
                elif field_name in {
                    "class_contribution",
                    "class_contribution_uncertainty",
                }:
                    if any(
                        not isinstance(series, list) or len(series) != len(epochs)
                        for series in values
                    ):
                        raise StateError(
                            422, f"Each {field_name} series must match epochs"
                        )
                    if len(values) != len(state.classes):
                        raise StateError(
                            422,
                            f"{field_name} series must match the class mapping",
                        )
                    if any(
                        value is not None and not self._finite_number(value)
                        for series in values
                        for value in series
                    ):
                        raise StateError(
                            422, f"{field_name} evidence must be finite numbers or null"
                        )
                elif field_name in {
                    "activation_mean",
                    "activation_variance",
                    "inactive_fraction",
                    "gradient_mean",
                    "gradient_variance",
                    "weight_norm",
                } and any(
                    value is not None and not self._finite_number(value)
                    for value in values
                ):
                    raise StateError(
                        422, f"Evidence field {field_name} must contain finite numbers or null"
                    )

            live_rows = {
                row["epoch"]: row["metrics"]
                for row in state.live_neuron_buffers.get(neuron_id, ())
            }
            for field_name in (
                "activation_mean",
                "activation_variance",
                "inactive_fraction",
                "gradient_mean",
                "gradient_variance",
                "weight_norm",
            ):
                values = evidence.get(field_name)
                if values is None:
                    continue
                for epoch, value in zip(epochs, values):
                    live_metrics = live_rows.get(epoch)
                    if (
                        live_metrics is None
                        or value is None
                        or live_metrics.get(field_name) is None
                    ):
                        continue
                    if not math.isclose(
                        value,
                        live_metrics[field_name],
                        rel_tol=1e-6,
                        abs_tol=1e-7,
                    ):
                        raise StateError(
                            422,
                            f"Evidence {field_name} conflicts with live data at epoch {epoch}",
                        )

            for field_name in (
                "class_contribution",
                "class_contribution_uncertainty",
            ):
                contributions = evidence.get(field_name)
                if contributions is None:
                    continue
                for epoch_index, epoch in enumerate(epochs):
                    live_metrics = live_rows.get(epoch)
                    live_values = (
                        live_metrics.get(field_name)
                        if live_metrics is not None
                        else None
                    )
                    if live_values is None:
                        continue
                    for class_index, series in enumerate(contributions):
                        value = series[epoch_index]
                        live_value = live_values[class_index]
                        if value is None or live_value is None:
                            continue
                        if not math.isclose(
                            value, live_value, rel_tol=1e-6, abs_tol=1e-7
                        ):
                            raise StateError(
                                422,
                                f"Evidence {field_name} conflicts with live data "
                                f"at epoch {epoch}",
                            )
        return copy.deepcopy(evidence)

    @staticmethod
    def _snapshot_neuron(
        state: ExperimentState, neuron_id: str
    ) -> dict[str, Any]:
        rows = list(state.live_neuron_buffers.get(neuron_id, ()))
        return {
            "epochs": [row["epoch"] for row in rows],
            "metrics": [copy.deepcopy(row["metrics"]) for row in rows],
        }

    def snapshot(self, experiment_id: str, project_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.get(experiment_id, project_id)
            return {
                "type": "snapshot",
                "experiment_id": state.experiment_id,
                "name": state.name,
                "framework": state.framework,
                "model_name": state.model_name,
                "status": "finished" if state.finished else "running",
                "revision": state.revision,
                "last_activity": state.last_activity_at,
                "architecture": copy.deepcopy(state.architecture),
                "current_epoch": state.current_epoch,
                "epochs": list(state.epochs),
                "pending_epoch": (
                    [
                        {
                            "epoch": epoch,
                            "received_chunks": len(staged.chunks),
                            "chunk_count": staged.chunk_count,
                        }
                        for epoch, staged in state.staged_epochs.items()
                    ][0]
                    if state.staged_epochs
                    else None
                ),
                "live_neuron_buffers": {
                    neuron_id: copy.deepcopy(list(rows))
                    for neuron_id, rows in state.live_neuron_buffers.items()
                },
                "collapse_events": copy.deepcopy(list(state.collapse_events.values())),
            }

    def summaries(self, project_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                state.experiment_id: {
                    "status": "finished" if state.finished else "running",
                    "current_epoch": state.current_epoch,
                }
                for state in self.active_experiments.values()
                if state.project_id == project_id
            }

    def list_events(self, experiment_id: str, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            state = self.get(experiment_id, project_id)
            return copy.deepcopy(list(state.collapse_events.values()))

    def get_event(
        self, experiment_id: str, project_id: str, event_id: str
    ) -> dict[str, Any]:
        with self._lock:
            state = self.get(experiment_id, project_id)
            event = state.collapse_events.get(event_id)
            if event is None:
                raise StateError(404, "Collapse event not found")
            return copy.deepcopy(event)

    def finish_experiment(self, experiment_id: str, project_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.get(experiment_id, project_id)
            if state.staged_epochs:
                raise StateError(409, "Cannot finish with an incomplete staged epoch")
            state.finished = True
            result = {
                "experiment_id": experiment_id,
                "status": "finished",
                "current_epoch": state.current_epoch,
            }
            self._publish_locked(
                experiment_id,
                {"type": "experiment_finished", **result},
            )
            return result

    def clear_live(self, experiment_id: str, project_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.get(experiment_id, project_id)
            removed = sum(len(rows) for rows in state.live_neuron_buffers.values())
            state.live_neuron_buffers.clear()
            state.live_row_sizes.clear()
            state.epochs.clear()
            state.recent_epoch_digests.clear()
            state.staged_epochs.clear()
            state.completed_chunk_epochs.clear()
            state.neuron_layers = {
                neuron_id: layer_id
                for neuron_id, layer_id in state.neuron_layers.items()
                if state.layers.get(layer_id, {}).get("neuron_ids") is not None
            }
            state.live_memory_bytes = 0
            self._publish_locked(
                experiment_id,
                {"type": "live_cleared", "experiment_id": experiment_id},
            )
            return {"status": "cleared", "removed_records": removed}

    def clear_events(self, experiment_id: str, project_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.get(experiment_id, project_id)
            count = len(state.collapse_events)
            state.collapse_events.clear()
            state.event_sizes.clear()
            state.event_requests.clear()
            state.unresolved_events.clear()
            state.event_memory_bytes = 0
            self._publish_locked(
                experiment_id,
                {"type": "events_cleared", "experiment_id": experiment_id},
            )
            return {"status": "cleared", "removed_events": count}

    def delete_experiment(self, experiment_id: str, project_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.get(experiment_id, project_id)
            self._publish_locked(
                experiment_id,
                {"type": "experiment_deleted", "experiment_id": experiment_id},
            )
            del self.active_experiments[experiment_id]
            return {"status": "deleted", "experiment_id": experiment_id}

    def subscribe(self, experiment_id: str, project_id: str) -> Subscriber:
        with self._lock:
            self.get(experiment_id, project_id)
            subscribers = self._subscribers.setdefault(experiment_id, {})
            if len(subscribers) >= settings.MAX_SUBSCRIBERS_PER_EXPERIMENT:
                raise StateError(429, "Maximum WebSocket subscriber capacity reached")
            subscriber = Subscriber(
                subscriber_id=str(uuid4()),
                loop=asyncio.get_running_loop(),
                queue=asyncio.Queue(maxsize=2),
            )
            subscribers[subscriber.subscriber_id] = subscriber
            return subscriber

    def unsubscribe(self, experiment_id: str, subscriber_id: str) -> None:
        with self._lock:
            subscribers = self._subscribers.get(experiment_id)
            if subscribers is None:
                return
            subscribers.pop(subscriber_id, None)
            if not subscribers:
                self._subscribers.pop(experiment_id, None)

    def _publish_locked(self, experiment_id: str, message: dict[str, Any]) -> None:
        state = self.active_experiments.get(experiment_id)
        if state is not None:
            state.revision += 1
            message = {**message, "revision": state.revision}
        subscribers = tuple(self._subscribers.get(experiment_id, {}).values())
        for subscriber in subscribers:
            def enqueue(target: Subscriber = subscriber, item=message) -> None:
                if target.queue.full():
                    while not target.queue.empty():
                        target.queue.get_nowait()
                    target.queue.put_nowait({"type": "resync_required"})
                    return
                target.queue.put_nowait(copy.deepcopy(item))

            subscriber.loop.call_soon_threadsafe(enqueue)

    @staticmethod
    def _touch(state: ExperimentState) -> None:
        state.last_activity = time.monotonic()
        state.last_activity_at = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _json_size(value: Any) -> int:
        return len(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )

    @staticmethod
    def _staged_memory_bytes(state: ExperimentState) -> int:
        return sum(staged.size_bytes for staged in state.staged_epochs.values())

    @staticmethod
    def _finite_number(value: Any) -> bool:
        if type(value) not in (int, float):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    def expire_idle(self) -> list[str]:
        expired: list[str] = []
        cutoff = time.monotonic() - settings.EXPERIMENT_IDLE_SECONDS
        with self._lock:
            for experiment_id, state in list(self.active_experiments.items()):
                if state.last_activity < cutoff:
                    self._publish_locked(
                        experiment_id,
                        {"type": "experiment_expired", "experiment_id": experiment_id},
                    )
                    del self.active_experiments[experiment_id]
                    expired.append(experiment_id)
        return expired


live_state = LiveState()
