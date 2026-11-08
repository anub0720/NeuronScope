"""SDK and collapse-rule configuration."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from neuronscope.exceptions import ConfigurationError


@dataclass(frozen=True)
class CollapseRule:
    rule_id: str
    metric: Literal[
        "class_contribution",
        "gradient_mean",
        "gradient_variance",
        "weight_norm",
        "activation_variance",
    ]
    operator: Literal["below", "above"]
    threshold: float
    consecutive_epochs: int = 2
    affected_classes: tuple[int, ...] = ()
    recovery_threshold: float | None = None

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ConfigurationError("collapse rule_id must not be empty")
        if len(self.rule_id) > 80:
            raise ConfigurationError("collapse rule_id must not exceed 80 characters")
        if self.metric not in {
            "class_contribution",
            "gradient_mean",
            "gradient_variance",
            "weight_norm",
            "activation_variance",
        }:
            raise ConfigurationError("unsupported collapse metric")
        if self.operator not in {"below", "above"}:
            raise ConfigurationError("collapse operator must be 'below' or 'above'")
        if not math.isfinite(self.threshold) or (
            self.recovery_threshold is not None
            and not math.isfinite(self.recovery_threshold)
        ):
            raise ConfigurationError("collapse thresholds must be finite")
        if type(self.consecutive_epochs) is not int or self.consecutive_epochs < 1:
            raise ConfigurationError("consecutive_epochs must be at least one")
        if self.metric != "class_contribution" and self.affected_classes:
            raise ConfigurationError(
                "affected_classes are only supported for class_contribution rules"
            )
        if any(index < 0 for index in self.affected_classes):
            raise ConfigurationError("affected class indices must be non-negative")
        if len(set(self.affected_classes)) != len(self.affected_classes):
            raise ConfigurationError("affected class indices must be unique")


@dataclass(frozen=True)
class MonitorConfig:
    window_size: int = 5
    collect_activations: bool = True
    collect_gradients: bool = True
    collect_weights: bool = True
    collect_contributions: bool = False
    contribution_neurons: tuple[tuple[str, int], ...] = ()
    max_contribution_neurons: int = 8
    contribution_probe_batches: int = 1
    event_evidence_window_size: int = 5
    quantiles: tuple[float, ...] = ()
    quantile_samples_per_neuron: int = 64
    inactive_epsilon: float = 0.0
    retry_queue_size: int = 32
    retry_queue_bytes: int = 8 * 1024 * 1024
    event_queue_size: int = 512
    event_queue_bytes: int = 4 * 1024 * 1024
    max_event_bytes: int = 4 * 1024 * 1024
    max_event_history: int = 256
    request_timeout: float = 10.0
    retry_max_delay: float = 30.0
    flush_timeout: float = 5.0
    max_request_bytes: int = 900 * 1024
    collapse_rules: tuple[CollapseRule, ...] = ()

    def __post_init__(self) -> None:
        if self.window_size < 1 or self.window_size > 100:
            raise ConfigurationError("window_size must be between 1 and 100")
        if self.retry_queue_size < 3 or self.retry_queue_bytes < 1024:
            raise ConfigurationError(
                "retry_queue_size must be at least three for initialization"
            )
        if self.event_queue_size < 1 or self.event_queue_bytes < 1024:
            raise ConfigurationError("event queue limits must be positive")
        if self.max_event_bytes < 1024:
            raise ConfigurationError("max_event_bytes must be at least 1024")
        if self.max_event_history < 1:
            raise ConfigurationError("max_event_history must be positive")
        if not 1 <= self.event_evidence_window_size <= 100:
            raise ConfigurationError(
                "event_evidence_window_size must be between 1 and 100"
            )
        if self.max_contribution_neurons < 0 or self.contribution_probe_batches < 1:
            raise ConfigurationError("invalid contribution collection limits")
        if len(self.contribution_neurons) > self.max_contribution_neurons:
            raise ConfigurationError(
                "contribution_neurons cannot exceed max_contribution_neurons"
            )
        if any(not layer_id or index < 0 for layer_id, index in self.contribution_neurons):
            raise ConfigurationError("contribution neuron references are invalid")
        if len(set(self.contribution_neurons)) != len(self.contribution_neurons):
            raise ConfigurationError("contribution_neurons must be unique")
        if self.quantile_samples_per_neuron < 1:
            raise ConfigurationError("quantile_samples_per_neuron must be positive")
        if any(not 0 < quantile < 1 for quantile in self.quantiles):
            raise ConfigurationError("quantiles must be strictly between 0 and 1")
        if len(set(self.quantiles)) != len(self.quantiles):
            raise ConfigurationError("quantiles must be unique")
        if not math.isfinite(self.inactive_epsilon):
            raise ConfigurationError("inactive_epsilon must be finite")
        if self.inactive_epsilon < 0:
            raise ConfigurationError("inactive_epsilon must be non-negative")
        if (
            self.request_timeout <= 0
            or self.retry_max_delay <= 0
            or self.flush_timeout < 0
        ):
            raise ConfigurationError("timeouts must be positive")
        if not all(
            math.isfinite(timeout)
            for timeout in (
                self.request_timeout,
                self.retry_max_delay,
                self.flush_timeout,
            )
        ):
            raise ConfigurationError("timeouts must be finite")
        if self.max_request_bytes < 1024:
            raise ConfigurationError("max_request_bytes must be at least 1024")
        rule_ids = [rule.rule_id for rule in self.collapse_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ConfigurationError("collapse rule IDs must be unique")
