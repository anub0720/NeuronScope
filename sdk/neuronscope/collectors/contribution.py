"""Opt-in class-conditional neuron-ablation diagnostics."""

from __future__ import annotations

import math
from typing import Any, Iterable

from neuronscope.architecture import LayerInfo
from neuronscope.config import MonitorConfig
from neuronscope.exceptions import UnsupportedModelError

class ContributionCollector:
    def __init__(
        self,
        model: Any,
        layers: tuple[LayerInfo, ...],
        class_count: int,
        config: MonitorConfig,
    ) -> None:
        self.model = model
        self.layers = layers
        self.class_count = class_count
        self.config = config

    def selected_neurons(self) -> list[tuple[LayerInfo, int]]:
        layers_by_id = {layer.layer_id: layer for layer in self.layers}
        if self.config.contribution_neurons:
            selected = []
            for layer_id, index in self.config.contribution_neurons:
                layer = layers_by_id.get(layer_id)
                if layer is None or index < 0 or index >= layer.neuron_count:
                    raise ValueError(f"Unknown contribution neuron {layer_id}:{index}")
                selected.append((layer, index))
            return selected
        result: list[tuple[LayerInfo, int]] = []
        limit = self.config.max_contribution_neurons
        for layer in self.layers:
            for index in range(layer.neuron_count):
                if len(result) >= limit:
                    return result
                result.append((layer, index))
        return result

    def collect(self, probe_loader: Iterable[Any]) -> dict[str, dict[str, Any]]:
        import torch
        import torch.nn.functional as functional

        selected = self.selected_neurons()
        if not selected:
            return {}

        module_modes = [(module, module.training) for module in self.model.modules()]
        cpu_rng = torch.random.get_rng_state()
        cuda_rng = torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None
        totals = {
            (layer.layer_id, index): {
                "sum": [0.0] * self.class_count,
                "sum_squares": [0.0] * self.class_count,
                "counts": [0] * self.class_count,
            }
            for layer, index in selected
        }
        batch_count = 0
        try:
            self.model.eval()
            with torch.inference_mode():
                for batch in probe_loader:
                    inputs, labels = self._unpack_batch(batch)
                    device = self._model_device()
                    inputs = self._move_to_device(inputs, device)
                    labels = labels.to(device=device, dtype=torch.long)
                    if labels.ndim != 1:
                        raise UnsupportedModelError(
                            "Class contribution requires one integer class label per example"
                        )
                    baseline_logits = self.model(inputs)
                    if not isinstance(baseline_logits, torch.Tensor):
                        raise UnsupportedModelError(
                            "Class contribution requires a tensor of classification logits"
                        )
                    if (
                        baseline_logits.ndim != 2
                        or baseline_logits.shape[0] != labels.shape[0]
                        or baseline_logits.shape[1] != self.class_count
                    ):
                        raise UnsupportedModelError(
                            "Class contribution requires [batch, class_count] logits"
                        )
                    if labels.numel() and (
                        int(labels.min()) < 0 or int(labels.max()) >= self.class_count
                    ):
                        raise ValueError(
                            "Probe labels must use the registered class indices"
                        )
                    baseline_losses = functional.cross_entropy(
                        baseline_logits.float(), labels, reduction="none"
                    )

                    for layer, neuron_index in selected:
                        def ablate(_module, _module_inputs, output):
                            if not isinstance(output, torch.Tensor):
                                raise UnsupportedModelError(
                                    "Ablation requires tensor outputs from supported layers"
                                )
                            axis = output.ndim - 1 if layer.module_type == "Linear" else 1
                            if output.shape[axis] != layer.neuron_count:
                                raise UnsupportedModelError(
                                    f"Layer {layer.layer_id} produced an unexpected output shape"
                                )
                            result = output.clone()
                            index = [slice(None)] * result.ndim
                            index[axis] = neuron_index
                            result[tuple(index)] = 0
                            return result

                        handle = layer.module.register_forward_hook(ablate)
                        try:
                            ablated_logits = self.model(inputs)
                        finally:
                            handle.remove()
                        if not isinstance(ablated_logits, torch.Tensor):
                            raise UnsupportedModelError(
                                "Ablation requires tensor classification logits"
                            )
                        ablated_losses = functional.cross_entropy(
                            ablated_logits.float(), labels, reduction="none"
                        )
                        differences = (ablated_losses - baseline_losses).cpu()
                        stats = totals[(layer.layer_id, neuron_index)]
                        for class_index in range(self.class_count):
                            values = differences[labels.cpu() == class_index]
                            if values.numel() == 0:
                                continue
                            stats["sum"][class_index] += float(values.sum())
                            stats["sum_squares"][class_index] += float(
                                torch.sum(values * values)
                            )
                            stats["counts"][class_index] += int(values.numel())
                    batch_count += 1
                    if batch_count >= self.config.contribution_probe_batches:
                        break
        finally:
            for module, training in module_modes:
                module.training = training
            torch.random.set_rng_state(cpu_rng)
            if cuda_rng is not None:
                torch.cuda.set_rng_state_all(cuda_rng)

        metrics: dict[str, dict[str, Any]] = {}
        for layer, index in selected:
            stats = totals[(layer.layer_id, index)]
            contributions: list[float | None] = []
            counts: list[int | None] = []
            validity: list[bool] = []
            uncertainties: list[float | None] = []
            standard_errors: list[float] = []
            for class_index, count in enumerate(stats["counts"]):
                counts.append(count or None)
                validity.append(count > 0)
                if not count:
                    contributions.append(None)
                    uncertainties.append(None)
                    continue
                mean = stats["sum"][class_index] / count
                contributions.append(mean if math.isfinite(mean) else None)
                if count > 1:
                    variance = max(
                        0.0,
                        (
                            stats["sum_squares"][class_index]
                            - stats["sum"][class_index] ** 2 / count
                        )
                        / (count - 1),
                    )
                    standard_error = math.sqrt(variance / count)
                    uncertainties.append(standard_error)
                    standard_errors.append(standard_error)
                else:
                    uncertainties.append(None)
            neuron_id = layer.neuron_ids[index]
            row: dict[str, Any] = {
                "class_contribution": contributions,
                "class_sample_counts": counts,
                "class_validity_mask": validity,
                "class_contribution_uncertainty": uncertainties,
            }
            if standard_errors:
                row["uncertainty"] = sum(standard_errors) / len(standard_errors)
            metrics[neuron_id] = row
        return metrics

    @staticmethod
    def _unpack_batch(batch: Any) -> tuple[Any, Any]:
        if not isinstance(batch, (tuple, list)) or len(batch) < 2:
            raise UnsupportedModelError(
                "probe_loader must yield (inputs, integer_labels)"
            )
        return batch[0], batch[1]

    def _model_device(self):
        parameters = iter(self.model.parameters())
        first = next(parameters, None)
        if first is None:
            raise UnsupportedModelError("Cannot infer device for a parameter-free model")
        return first.device

    @classmethod
    def _move_to_device(cls, value: Any, device: Any) -> Any:
        import torch

        if isinstance(value, torch.Tensor):
            return value.to(device=device)
        if isinstance(value, tuple):
            return tuple(cls._move_to_device(item, device) for item in value)
        if isinstance(value, list):
            return [cls._move_to_device(item, device) for item in value]
        if isinstance(value, dict):
            return {
                key: cls._move_to_device(item, device)
                for key, item in value.items()
            }
        raise UnsupportedModelError(
            "Probe inputs must be tensors or containers of tensors"
        )
