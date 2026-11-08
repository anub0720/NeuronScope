"""Per-neuron weight summaries."""

from __future__ import annotations

from typing import Any


def collect_weight_metrics(layers: tuple[Any, ...]) -> dict[str, dict[str, float]]:
    import torch

    output: dict[str, dict[str, float]] = {}
    with torch.no_grad():
        for layer in layers:
            module = layer.module
            weight = module.weight.detach().float().reshape(layer.neuron_count, -1)
            norms = torch.linalg.vector_norm(weight, ord=2, dim=1).cpu().tolist()
            for index, neuron_id in enumerate(layer.neuron_ids):
                output[neuron_id] = {"weight_norm": float(norms[index])}
    return output
