"""Bounded epoch and event history."""

from __future__ import annotations

import copy
import threading
from collections import deque
from typing import Any

from neuronscope.exceptions import ConfigurationError


class MetricBuffer:
    def __init__(
        self,
        window_size: int,
        neuron_to_layer: dict[str, str],
        class_count: int,
    ) -> None:
        if window_size < 1:
            raise ConfigurationError("window_size must be positive")
        self._window: deque[dict[str, Any]] = deque(maxlen=window_size)
        self._neuron_to_layer = dict(neuron_to_layer)
        self._class_count = class_count
        self._last_epoch = -1
        self._lock = threading.RLock()

    @property
    def epochs(self) -> list[int]:
        with self._lock:
            return [record["epoch"] for record in self._window]

    @property
    def neuron_ids(self) -> tuple[str, ...]:
        return tuple(self._neuron_to_layer)

    def append(self, epoch: int, neurons: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if type(epoch) is not int or epoch < 0:
            raise ValueError("epoch must be a non-negative integer")
        with self._lock:
            if epoch <= self._last_epoch:
                raise ValueError("epoch numbers must increase monotonically")
            unknown = set(neurons) - self._neuron_to_layer.keys()
            if unknown:
                raise ValueError(f"unknown neuron IDs: {sorted(unknown)[:3]}")

            payload_neurons: dict[str, dict[str, Any]] = {}
            for neuron_id, layer_id in self._neuron_to_layer.items():
                metrics = copy.deepcopy(neurons.get(neuron_id, {}))
                metrics["layer_id"] = layer_id
                contribution = metrics.get("class_contribution")
                if contribution is not None and len(contribution) != self._class_count:
                    raise ValueError("class contribution must match class mapping")
                counts = metrics.get("class_sample_counts")
                if counts is not None and len(counts) != self._class_count:
                    raise ValueError("class sample counts must match class mapping")
                payload_neurons[neuron_id] = metrics

            record = {"epoch": epoch, "neurons": payload_neurons}
            self._window.append(copy.deepcopy(record))
            self._last_epoch = epoch
            return {
                "epoch": epoch,
                "neurons": copy.deepcopy(payload_neurons),
            }

    def latest(self, neuron_id: str) -> list[dict[str, Any]]:
        with self._lock:
            if neuron_id not in self._neuron_to_layer:
                raise KeyError(neuron_id)
            return [
                {
                    "epoch": record["epoch"],
                    "metrics": copy.deepcopy(record["neurons"][neuron_id]),
                }
                for record in self._window
            ]

    def epoch_payloads(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "experiment_id": experiment_id,
                    "epoch": record["epoch"],
                    "neurons": copy.deepcopy(record["neurons"]),
                }
                for record in self._window
            ]

    def epoch_payload(self, experiment_id: str, epoch: int) -> dict[str, Any] | None:
        with self._lock:
            for record in self._window:
                if record["epoch"] == epoch:
                    return {
                        "experiment_id": experiment_id,
                        "epoch": epoch,
                        "neurons": copy.deepcopy(record["neurons"]),
                    }
            return None

    def clear(self) -> None:
        with self._lock:
            self._window.clear()
            self._last_epoch = -1
