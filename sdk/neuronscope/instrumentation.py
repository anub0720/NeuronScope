"""Non-invasive hooks for per-neuron activation and gradient collection."""

from __future__ import annotations

import logging
from typing import Any

from neuronscope.architecture import LayerInfo
from neuronscope.collectors.activation import RunningNeuronStats
from neuronscope.config import MonitorConfig

logger = logging.getLogger("neuronscope")


class Instrumentation:
    def __init__(self, layers: tuple[LayerInfo, ...], config: MonitorConfig) -> None:
        self.layers = layers
        self.config = config
        self.collecting = True
        self.handles: list[Any] = []
        self.activations: dict[str, RunningNeuronStats] = {}
        self.gradients: dict[str, RunningNeuronStats] = {}

    def install(self) -> None:
        for layer in self.layers:
            self.activations[layer.layer_id] = self._new_stats(layer)
            self.gradients[layer.layer_id] = self._new_stats(layer)
            self.handles.append(
                layer.module.register_forward_hook(self._forward_hook(layer))
            )

    def _new_stats(self, layer: LayerInfo) -> RunningNeuronStats:
        return RunningNeuronStats(
            neuron_count=layer.neuron_count,
            inactive_epsilon=self.config.inactive_epsilon,
            quantiles=self.config.quantiles,
            quantile_samples_per_neuron=self.config.quantile_samples_per_neuron,
        )

    def _forward_hook(self, layer: LayerInfo):
        feature_axis = -1 if layer.module_type == "Linear" else 1

        def capture(_module: Any, _inputs: tuple[Any, ...], output: Any) -> None:
            if not self.collecting:
                return
            try:
                import torch

                if not isinstance(output, torch.Tensor):
                    return
                if self.config.collect_activations:
                    self.activations[layer.layer_id].add(output, feature_axis)
                if (
                    self.config.collect_gradients
                    and output.requires_grad
                    and torch.is_grad_enabled()
                ):
                    output.register_hook(self._gradient_hook(layer, feature_axis))
            except Exception:
                logger.exception("Could not collect metrics for layer %s", layer.layer_id)

        return capture

    def _gradient_hook(self, layer: LayerInfo, feature_axis: int):
        def capture(gradient: Any) -> Any:
            if self.collecting:
                try:
                    self.gradients[layer.layer_id].add(
                        gradient, feature_axis, absolute=True
                    )
                except Exception:
                    logger.exception(
                        "Could not collect gradients for layer %s", layer.layer_id
                    )
            return gradient

        return capture

    def reset_epoch(self) -> None:
        self.activations = {
            layer.layer_id: self._new_stats(layer) for layer in self.layers
        }
        self.gradients = {
            layer.layer_id: self._new_stats(layer) for layer in self.layers
        }

    def remove(self) -> None:
        self.collecting = False
        for handle in self.handles:
            handle.remove()
        self.handles.clear()
        self.activations.clear()
        self.gradients.clear()
