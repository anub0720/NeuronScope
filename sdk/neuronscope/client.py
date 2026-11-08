"""Synchronous monitor API backed by a background asynchronous HTTP client."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from neuronscope.architecture import (
    ModelArchitecture,
    discover_architecture,
    normalize_class_mapping,
)
from neuronscope.buffers import MetricBuffer
from neuronscope.collapse import CollapseDetector
from neuronscope.collectors.contribution import ContributionCollector
from neuronscope.collectors.weights import collect_weight_metrics
from neuronscope.config import MonitorConfig
from neuronscope.exceptions import ConfigurationError, UnsupportedModelError
from neuronscope.instrumentation import Instrumentation
from neuronscope.onboarding import resolve_credentials
from neuronscope.serialization import omit_missing
from neuronscope.transport import AsyncTransport

logger = logging.getLogger("neuronscope")


class Monitor:
    def __init__(
        self,
        model: Any,
        project_id: str,
        experiment_name: str,
        endpoint: str,
        api_key: str,
        class_mapping: Mapping[int | str, str],
        config: MonitorConfig | None = None,
        experiment_id: str | None = None,
        _http_transport: Any = None,
    ) -> None:
        self.config = config or MonitorConfig()
        self.project_id = self._required_text(project_id, "project_id")
        self.experiment_name = self._required_text(
            experiment_name, "experiment_name"
        )
        self.endpoint = self._validate_endpoint(endpoint)
        self.api_key = self._required_text(api_key, "api_key")
        self.class_mapping = normalize_class_mapping(dict(class_mapping))
        self.experiment_id = experiment_id or str(uuid4())
        if len(self.experiment_id) > 36 or not self.experiment_id.strip():
            raise ConfigurationError("experiment_id must contain 1 to 36 characters")
        self._validate_rules()
        self._model: Any | None = model
        self._architecture: ModelArchitecture = discover_architecture(model)
        self._architecture_identity = tuple(
            (
                layer.layer_id,
                layer.module_type,
                layer.neuron_count,
                id(layer.module),
            )
            for layer in self._architecture.layers
        )
        self._buffer = MetricBuffer(
            self.config.window_size,
            {
                neuron_id: layer.layer_id
                for layer in self._architecture.layers
                for neuron_id in layer.neuron_ids
            },
            len(self.class_mapping),
        )
        self._instrumentation = Instrumentation(
            self._architecture.layers, self.config
        )
        self._detector = CollapseDetector(
            self.config.collapse_rules,
            len(self.class_mapping),
            self.config.max_event_history,
            self.config.event_evidence_window_size,
            self.config.max_event_bytes,
        )
        self._lock = threading.RLock()
        self._closed = False
        self._finish_succeeded = False
        self._last_epoch = -1
        architecture_payload = self._architecture.to_payload(
            self.class_mapping, self.experiment_id
        )
        self._transport = AsyncTransport(
            endpoint=self.endpoint,
            api_key=self.api_key,
            experiment_id=self.experiment_id,
            experiment_name=self.experiment_name,
            model_name=self._architecture.model_name,
            architecture=architecture_payload,
            config=self.config,
            state_provider=self._synchronization_state,
            epoch_provider=self._epoch_payload,
            event_provider=self._pending_event_messages,
            event_acknowledger=self._detector.acknowledge,
            http_transport=_http_transport,
        )
        try:
            self._instrumentation.install()
            self._transport.start()
        except Exception:
            self._instrumentation.remove()
            self._transport.shutdown()
            self._model = None
            raise

    def log_epoch(self, epoch: int, probe_loader: Any = None) -> None:
        with self._lock:
            self._ensure_open()
            if type(epoch) is not int or epoch < 0:
                raise ValueError("epoch must be a non-negative integer")
            if epoch <= self._last_epoch:
                raise ValueError("epoch numbers must increase monotonically")
            self._check_architecture_unchanged()

            layer_metrics: dict[str, list[dict[str, Any] | None]] = {}
            if self.config.collect_activations:
                for layer in self._architecture.layers:
                    layer_metrics[layer.layer_id] = (
                        self._instrumentation.activations[layer.layer_id].metrics()
                    )

            gradient_metrics: dict[str, list[dict[str, Any] | None]] = {}
            if self.config.collect_gradients:
                for layer in self._architecture.layers:
                    gradient_metrics[layer.layer_id] = (
                        self._instrumentation.gradients[layer.layer_id].metrics()
                    )

            weights = {}
            if self.config.collect_weights:
                try:
                    weights = collect_weight_metrics(self._architecture.layers)
                except Exception:
                    logger.exception("Could not collect model weight summaries")

            neuron_metrics: dict[str, dict[str, Any]] = {
                neuron_id: {"layer_id": layer.layer_id}
                for layer in self._architecture.layers
                for neuron_id in layer.neuron_ids
            }
            for layer in self._architecture.layers:
                activations = layer_metrics.get(layer.layer_id, [])
                gradients = gradient_metrics.get(layer.layer_id, [])
                for index, neuron_id in enumerate(layer.neuron_ids):
                    metrics = neuron_metrics[neuron_id]
                    activation = activations[index] if index < len(activations) else None
                    if activation is not None:
                        metrics.update(
                            {
                                "activation_mean": activation["mean"],
                                "activation_variance": activation["variance"],
                                "inactive_fraction": activation["inactive_fraction"],
                                "sample_count": activation["sample_count"],
                            }
                        )
                        if "quantiles" in activation:
                            metrics["activation_quantiles"] = activation["quantiles"]
                    gradient = gradients[index] if index < len(gradients) else None
                    if gradient is not None:
                        metrics["gradient_mean"] = gradient["mean"]
                        metrics["gradient_variance"] = gradient["variance"]
                    metrics.update(weights.get(neuron_id, {}))

            if self.config.collect_contributions and probe_loader is not None:
                self._instrumentation.collecting = False
                try:
                    contributions = ContributionCollector(
                        self._model,
                        self._architecture.layers,
                        len(self.class_mapping),
                        self.config,
                    ).collect(probe_loader)
                    for neuron_id, metrics in contributions.items():
                        neuron_metrics[neuron_id].update(metrics)
                except Exception:
                    logger.exception(
                        "Could not collect class-wise contribution metrics"
                    )
                finally:
                    self._instrumentation.collecting = True

            self._instrumentation.reset_epoch()
            cleaned = {
                neuron_id: omit_missing(metrics)
                for neuron_id, metrics in neuron_metrics.items()
            }
            payload = self._buffer.append(epoch, cleaned)
            self._last_epoch = epoch
            histories = {
                neuron_id: self._buffer.latest(neuron_id)
                for neuron_id in self._buffer.neuron_ids
            }
            event_messages = self._detector.process(
                self.experiment_id, epoch, payload["neurons"], histories
            )
            payload["experiment_id"] = self.experiment_id
            self._transport.submit_epoch(payload)
            for event in event_messages:
                self._transport.submit_event(event)

            if self._detector.dropped_events:
                logger.warning(
                    "Collapse event retention failed %d time(s): %s",
                    self._detector.dropped_events,
                    self._detector.retention_error,
                )

    def status(self) -> dict[str, Any]:
        state = self._transport.status()
        with self._lock:
            state.update(
                {
                    "experiment_id": self.experiment_id,
                    "project_id": self.project_id,
                    "last_epoch": self._last_epoch,
                    "retained_epochs": self._buffer.epochs,
                    "retained_events": len(self._detector.events),
                    "dropped_events": self._detector.dropped_events,
                    "pending_events": len(self._detector.pending_messages()),
                    "event_retention_error": self._detector.retention_error,
                    "finished": self._closed,
                }
            )
        return state

    def finish(self, timeout: float | None = None) -> bool:
        with self._lock:
            if self._closed and self._finish_succeeded:
                return True
            if not self._closed:
                self._close_instrumentation()
                self._closed = True
        flushed = self._transport.finish(
            self.config.flush_timeout if timeout is None else timeout
        )
        if flushed:
            with self._lock:
                self._finish_succeeded = True
                self._release_local_buffers()
        return flushed

    def clear(self, timeout: float | None = None) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._close_instrumentation()
            self._closed = True
        flushed = self._transport.clear(
            self.config.flush_timeout if timeout is None else timeout
        )
        with self._lock:
            self._release_local_buffers()
        return flushed

    def history(self, neuron_id: str) -> list[dict[str, Any]]:
        return self._buffer.latest(neuron_id)

    def __enter__(self) -> "Monitor":
        self._ensure_open()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.finish()

    def _synchronization_state(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self._lock:
            epochs = self._buffer.epoch_payloads(self.experiment_id)
            events = self._detector.synchronization_messages()
            return epochs, events

    def _epoch_payload(self, epoch: int) -> dict[str, Any] | None:
        with self._lock:
            return self._buffer.epoch_payload(self.experiment_id, epoch)

    def _pending_event_messages(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._detector.pending_messages()

    def _validate_rules(self) -> None:
        class_count = len(self.class_mapping)
        for rule in self.config.collapse_rules:
            if any(index >= class_count for index in rule.affected_classes):
                raise ConfigurationError(
                    f"Collapse rule {rule.rule_id} references an unknown class"
                )

    def _check_architecture_unchanged(self) -> None:
        current = discover_architecture(self._model)
        identity = tuple(
            (
                layer.layer_id,
                layer.module_type,
                layer.neuron_count,
                id(layer.module),
            )
            for layer in current.layers
        )
        if identity != self._architecture_identity:
            raise UnsupportedModelError(
                "The model architecture changed after monitor initialization; "
                "the current backend cannot replace a registered architecture"
            )

    def _close_instrumentation(self) -> None:
        self._instrumentation.remove()

    def _release_local_buffers(self) -> None:
        self._buffer.clear()
        self._detector.clear()
        self._model = None
        self._architecture = ModelArchitecture(
            model_name="released", framework="pytorch", layers=()
        )
        self.api_key = ""

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Monitor is already finished or cleared")

    @staticmethod
    def _required_text(value: str, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"{field} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        if not isinstance(endpoint, str):
            raise ConfigurationError("endpoint must be a URL")
        parsed = urlparse(endpoint.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("endpoint must be an absolute HTTP(S) URL")
        return endpoint.rstrip("/")


def init(
    model: Any,
    project_id: str | None = None,
    experiment_name: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    class_mapping: Mapping[int | str, str] | None = None,
    config: MonitorConfig | None = None,
    experiment_id: str | None = None,
    project_name: str = "NeuronScope",
) -> Monitor:
    """Provision or reuse project credentials, then initialize the monitor."""
    if not experiment_name:
        raise ConfigurationError("experiment_name must be provided")
    if class_mapping is None:
        raise ConfigurationError("class_mapping must be provided")
    endpoint, project_id, api_key = resolve_credentials(
        endpoint, project_id, api_key, project_name
    )
    return Monitor(
        model=model,
        project_id=project_id,
        experiment_name=experiment_name,
        endpoint=endpoint,
        api_key=api_key,
        class_mapping=class_mapping,
        config=config,
        experiment_id=experiment_id,
    )
