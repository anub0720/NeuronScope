"""PyTorch layer discovery and backend-compatible serialization."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from neuronscope.exceptions import UnsupportedModelError


@dataclass(frozen=True)
class LayerInfo:
    layer_id: str
    module: Any
    module_type: str
    neuron_ids: tuple[str, ...]
    neuron_count: int
    position: int
    input_shape: tuple[int, ...] | None
    output_shape: tuple[int, ...] | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "type": self.module_type,
            "neuron_count": self.neuron_count,
            "neuron_ids": list(self.neuron_ids),
        }


@dataclass(frozen=True)
class ModelArchitecture:
    model_name: str
    framework: str
    layers: tuple[LayerInfo, ...]

    @property
    def neuron_to_layer(self) -> dict[str, LayerInfo]:
        return {
            neuron_id: layer
            for layer in self.layers
            for neuron_id in layer.neuron_ids
        }

    def to_payload(self, classes: dict[str, str], experiment_id: str) -> dict[str, Any]:
        return {
            "experiment_id": experiment_id,
            "version": 1,
            "framework": self.framework,
            "model_name": self.model_name,
            "classes": classes,
            "layers": [layer.to_payload() for layer in self.layers],
        }

    def fingerprint(self) -> tuple[tuple[str, str, int], ...]:
        return tuple(
            (layer.layer_id, layer.module_type, layer.neuron_count)
            for layer in self.layers
        )


def discover_architecture(model: Any) -> ModelArchitecture:
    try:
        import torch.nn as nn
    except ImportError as exc:
        raise UnsupportedModelError(
            "PyTorch is required to initialize NeuronScope"
        ) from exc

    supported = (nn.Linear, nn.Conv1d, nn.Conv2d, nn.Conv3d)
    layers: list[LayerInfo] = []
    seen_modules: set[int] = set()
    for name, module in model.named_modules():
        if not isinstance(module, supported) or id(module) in seen_modules:
            continue
        seen_modules.add(id(module))
        layer_id = name or "__root__"
        if len(layer_id) > 200:
            digest = hashlib.sha256(layer_id.encode("utf-8")).hexdigest()[:16]
            layer_id = f"layer-{digest}"
        neuron_count = int(module.out_features if isinstance(module, nn.Linear) else module.out_channels)
        digest = hashlib.sha256(layer_id.encode("utf-8")).hexdigest()[:12]
        neuron_ids = tuple(
            f"n-{digest}-{index}" for index in range(neuron_count)
        )
        input_shape = None
        output_shape = None
        if isinstance(module, nn.Linear):
            input_shape = (int(module.in_features),)
            output_shape = (neuron_count,)
        else:
            input_shape = (int(module.in_channels),)
            output_shape = (neuron_count,)
        layers.append(
            LayerInfo(
                layer_id=layer_id,
                module=module,
                module_type=type(module).__name__,
                neuron_ids=neuron_ids,
                neuron_count=neuron_count,
                position=len(layers),
                input_shape=input_shape,
                output_shape=output_shape,
            )
        )

    if not layers:
        raise UnsupportedModelError(
            "Model contains no supported nn.Linear or nn.Conv1d/2d/3d modules"
        )
    return ModelArchitecture(
        model_name=type(model).__name__,
        framework="pytorch",
        layers=tuple(layers),
    )


def normalize_class_mapping(mapping: dict[int | str, str]) -> dict[str, str]:
    if not mapping:
        raise ValueError("class_mapping must contain at least one class")
    if any(not isinstance(label, str) or not label.strip() for label in mapping.values()):
        raise ValueError("class_mapping labels must be non-empty strings")
    try:
        indexed = sorted((int(index), str(index), label) for index, label in mapping.items())
    except (TypeError, ValueError) as exc:
        raise ValueError("class_mapping keys must be integer indices") from exc
    if [index for index, _, _ in indexed] != list(range(len(indexed))):
        raise ValueError("class_mapping indices must be contiguous starting at 0")
    if any(str(index) != key for index, key, _ in indexed):
        raise ValueError("class_mapping keys must use canonical integer values")
    return {str(index): label for index, _, label in indexed}
