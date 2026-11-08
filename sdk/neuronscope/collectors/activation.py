"""Streaming per-neuron activation and gradient summaries."""

from __future__ import annotations

from typing import Any

import numpy as np


class RunningNeuronStats:
    def __init__(
        self,
        neuron_count: int,
        inactive_epsilon: float = 0.0,
        quantiles: tuple[float, ...] = (),
        quantile_samples_per_neuron: int = 64,
    ) -> None:
        self.neuron_count = neuron_count
        self.inactive_epsilon = inactive_epsilon
        self.quantiles = quantiles
        self.quantile_samples_per_neuron = quantile_samples_per_neuron
        self.count = np.zeros(neuron_count, dtype=np.int64)
        self.mean = np.zeros(neuron_count, dtype=np.float64)
        self.m2 = np.zeros(neuron_count, dtype=np.float64)
        self.minimum = np.full(neuron_count, np.inf, dtype=np.float64)
        self.maximum = np.full(neuron_count, -np.inf, dtype=np.float64)
        self.inactive_count = np.zeros(neuron_count, dtype=np.int64)
        self._quantile_samples = (
            [np.empty(0, dtype=np.float32) for _ in range(neuron_count)]
            if quantiles
            else None
        )
        self._rng = np.random.default_rng()

    def add(self, tensor: Any, feature_axis: int, absolute: bool = False) -> None:
        import torch

        if not isinstance(tensor, torch.Tensor) or tensor.ndim < 2:
            return
        axis = feature_axis % tensor.ndim
        if tensor.shape[axis] != self.neuron_count:
            return
        values = tensor.detach()
        if absolute:
            values = values.abs()
        matrix = values.movedim(axis, 0).reshape(self.neuron_count, -1)
        if matrix.shape[1] == 0:
            return
        matrix = matrix.float()
        batch_count = matrix.shape[1]
        batch_mean = matrix.mean(dim=1).to(dtype=torch.float64).cpu().numpy()
        batch_var = matrix.var(dim=1, unbiased=False).to(dtype=torch.float64).cpu().numpy()
        batch_min = matrix.amin(dim=1).to(dtype=torch.float64).cpu().numpy()
        batch_max = matrix.amax(dim=1).to(dtype=torch.float64).cpu().numpy()
        inactive = (
            matrix.abs() <= self.inactive_epsilon
        ).sum(dim=1).to(dtype=torch.int64).cpu().numpy()

        old_count = self.count.copy()
        total_count = old_count + batch_count
        delta = batch_mean - self.mean
        self.mean += delta * batch_count / total_count
        self.m2 += (
            batch_var * batch_count
            + delta * delta * old_count * batch_count / total_count
        )
        self.count = total_count
        self.minimum = np.minimum(self.minimum, batch_min)
        self.maximum = np.maximum(self.maximum, batch_max)
        self.inactive_count += inactive
        if self._quantile_samples is not None:
            self._update_quantiles(matrix)

    def _update_quantiles(self, matrix: Any) -> None:
        values = matrix.detach().cpu().numpy()
        limit = self.quantile_samples_per_neuron
        for index, row in enumerate(values):
            current = self._quantile_samples[index]
            take_count = min(len(row), limit)
            new_sample = (
                row
                if len(row) <= limit
                else row[self._rng.choice(len(row), take_count, replace=False)]
            )
            if len(current) == 0:
                self._quantile_samples[index] = np.asarray(
                    new_sample, dtype=np.float32
                )
                continue
            combined = np.concatenate((current, new_sample))
            old_weight = self.count[index] - len(row)
            weights = np.concatenate(
                (
                    np.full(len(current), old_weight / len(current)),
                    np.full(len(new_sample), len(row) / len(new_sample)),
                )
            )
            weights = weights / weights.sum()
            sample_indices = self._rng.choice(
                len(combined), size=min(limit, len(combined)), replace=True, p=weights
            )
            self._quantile_samples[index] = combined[sample_indices]

    def metrics(self) -> list[dict[str, Any] | None]:
        rows: list[dict[str, Any] | None] = []
        variance = np.divide(
            self.m2,
            self.count,
            out=np.zeros_like(self.m2),
            where=self.count > 0,
        )
        for index in range(self.neuron_count):
            if self.count[index] == 0:
                rows.append(None)
                continue
            row: dict[str, Any] = {
                "sample_count": int(self.count[index]),
                "mean": float(self.mean[index]),
                "variance": float(variance[index]),
                "minimum": float(self.minimum[index]),
                "maximum": float(self.maximum[index]),
                "inactive_fraction": float(
                    self.inactive_count[index] / self.count[index]
                ),
            }
            if self._quantile_samples is not None and len(self._quantile_samples[index]):
                quantile_values = np.quantile(
                    self._quantile_samples[index], self.quantiles
                )
                row["quantiles"] = {
                    str(quantile): float(value)
                    for quantile, value in zip(self.quantiles, quantile_values)
                }
            rows.append(row)
        return rows
