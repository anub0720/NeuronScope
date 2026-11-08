"""Configurable collapse detection and independent evidence snapshots."""

from __future__ import annotations

import copy
import json
from collections import deque
from typing import Any
from uuid import uuid4

from neuronscope.config import CollapseRule


class CollapseDetector:
    def __init__(
        self,
        rules: tuple[CollapseRule, ...],
        class_count: int,
        max_event_history: int,
        evidence_window_size: int = 5,
        max_event_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.rules = rules
        self.class_count = class_count
        self.max_event_history = max_event_history
        self.evidence_window_size = evidence_window_size
        self.max_event_bytes = max_event_bytes
        self.active: dict[tuple[str, str, int | None], dict[str, Any]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=max_event_history)
        self._event_sizes: dict[str, int] = {}
        self._event_bytes = 0
        self._pending_messages: dict[str, dict[str, Any]] = {}
        self.dropped_events = 0
        self.retention_error: str | None = None

    def process(
        self,
        experiment_id: str,
        epoch: int,
        neurons: dict[str, dict[str, Any]],
        histories: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for rule in self.rules:
            for neuron_id, metrics in neurons.items():
                class_indices = (
                    rule.affected_classes
                    if rule.affected_classes
                    else tuple(range(self.class_count))
                    if rule.metric == "class_contribution"
                    else (None,)
                )
                for class_index in class_indices:
                    key = (rule.rule_id, neuron_id, class_index)
                    value = self._value(metrics, rule.metric, class_index)
                    if key not in self.active:
                        if self._triggered(
                            rule, value, histories[neuron_id], class_index
                        ):
                            event = self._make_event(
                                experiment_id,
                                rule,
                                neuron_id,
                                class_index,
                                epoch,
                                histories[neuron_id],
                            )
                            if self._retain_event(event):
                                self.active[key] = event
                                self._pending_messages[event["event_id"]] = copy.deepcopy(
                                    event
                                )
                                messages.append(copy.deepcopy(event))
                    elif self._recovered(rule, value):
                        active_event = self.active.pop(key)
                        recovery_message = {
                            "experiment_id": active_event["experiment_id"],
                            "event_id": f"{active_event['event_id']}:recovery",
                            "neuron_id": neuron_id,
                            "layer_id": active_event["layer_id"],
                            "event_type": active_event["event_type"],
                            "affected_classes": active_event["affected_classes"],
                            "detected_epoch": epoch,
                            "evidence": None,
                            "rule_version": active_event["rule_version"],
                            "status": "recovered",
                            "recovery": {
                                "epoch": epoch,
                                "metric": rule.metric,
                                "value": value,
                            },
                        }
                        for event in self.events:
                            if event["event_id"] == active_event["event_id"]:
                                event["status"] = "recovered"
                                event["recovered_epoch"] = epoch
                                event["recovery"] = copy.deepcopy(
                                    recovery_message["recovery"]
                                )
                                break
                        messages.append(recovery_message)
                        self._pending_messages[
                            recovery_message["event_id"]
                        ] = copy.deepcopy(recovery_message)
        return messages

    def history_snapshot(
        self, history: list[dict[str, Any]]
    ) -> dict[str, Any]:
        history = history[-self.evidence_window_size :]
        epochs = [row["epoch"] for row in history]
        result: dict[str, Any] = {"epochs": epochs}
        scalar_metrics = (
            "activation_mean",
            "activation_variance",
            "activation_quantiles",
            "inactive_fraction",
            "gradient_mean",
            "gradient_variance",
            "weight_norm",
            "sample_count",
            "uncertainty",
        )
        for metric in scalar_metrics:
            result[metric] = [
                (row["metrics"] or {}).get(metric) for row in history
            ]
        contribution_present = any(
            (row["metrics"] or {}).get("class_contribution") is not None
            for row in history
        )
        if contribution_present:
            result["class_contribution"] = [
                [
                    (
                        (row["metrics"] or {}).get("class_contribution") or [None] * self.class_count
                    )[class_index]
                    for row in history
                ]
                for class_index in range(self.class_count)
            ]
            result["class_contribution_uncertainty"] = [
                [
                    (
                        (
                            row["metrics"] or {}
                        ).get("class_contribution_uncertainty")
                        or [None] * self.class_count
                    )[class_index]
                    for row in history
                ]
                for class_index in range(self.class_count)
            ]
        return result

    def clear(self) -> None:
        self.active.clear()
        self.events.clear()
        self._event_sizes.clear()
        self._event_bytes = 0
        self._pending_messages.clear()
        self.dropped_events = 0
        self.retention_error = None

    def pending_messages(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self._pending_messages.values()))

    def acknowledge(self, event_id: str) -> None:
        self._pending_messages.pop(event_id, None)

    def _retain_event(self, event: dict[str, Any]) -> bool:
        event_size = len(
            json.dumps(event, separators=(",", ":"), allow_nan=False).encode("utf-8")
        )
        if event_size > self.max_event_bytes:
            self.dropped_events += 1
            self.retention_error = (
                f"Collapse event {event['event_id']} exceeds max_event_bytes"
            )
            return False
        if len(self.events) >= self.max_event_history:
            removable = next(
                (
                    index
                    for index, existing in enumerate(self.events)
                    if existing["status"] == "recovered"
                    and existing["event_id"] not in self._pending_messages
                    and f"{existing['event_id']}:recovery"
                    not in self._pending_messages
                ),
                None,
            )
            if removable is None:
                self.dropped_events += 1
                self.retention_error = (
                    "Collapse event history is full of unresolved events; "
                    "increase max_event_history or explicitly clear events"
                )
                return False
            removed = self.events[removable]
            del self.events[removable]
            self._event_bytes -= self._event_sizes.pop(removed["event_id"], 0)
        if self._event_bytes + event_size > self.max_event_bytes:
            self.dropped_events += 1
            self.retention_error = (
                "Collapse event evidence reached max_event_bytes; "
                "increase the limit or explicitly clear events"
            )
            return False
        self.events.append(copy.deepcopy(event))
        self._event_sizes[event["event_id"]] = event_size
        self._event_bytes += event_size
        self.retention_error = None
        return True

    def synchronization_messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for event in self.events:
            active = copy.deepcopy(event)
            if active["status"] == "recovered":
                active["status"] = "active"
                active["recovery"] = None
                active.pop("recovered_epoch", None)
            messages.append(active)
            if event["status"] == "recovered":
                messages.append(
                    {
                        "experiment_id": event["experiment_id"],
                        "event_id": f"{event['event_id']}:recovery",
                        "neuron_id": event["neuron_id"],
                        "layer_id": event["layer_id"],
                        "event_type": event["event_type"],
                        "affected_classes": event["affected_classes"],
                        "detected_epoch": event["recovered_epoch"],
                        "evidence": None,
                        "rule_version": event["rule_version"],
                        "status": "recovered",
                        "recovery": copy.deepcopy(event["recovery"]),
                    }
                )
        return messages

    def _triggered(
        self,
        rule: CollapseRule,
        current_value: float | None,
        history: list[dict[str, Any]],
        class_index: int | None,
    ) -> bool:
        if current_value is None:
            return False
        recent_values = [
            self._value(row["metrics"] or {}, rule.metric, class_index)
            for row in history[-rule.consecutive_epochs :]
        ]
        if len(recent_values) < rule.consecutive_epochs or any(
            value is None for value in recent_values
        ):
            return False
        return all(self._condition(rule.operator, value, rule.threshold) for value in recent_values)

    def _recovered(self, rule: CollapseRule, value: float | None) -> bool:
        if value is None:
            return False
        threshold = (
            rule.recovery_threshold
            if rule.recovery_threshold is not None
            else rule.threshold
        )
        return not self._condition(rule.operator, value, threshold)

    @staticmethod
    def _condition(operator: str, value: float, threshold: float) -> bool:
        return value < threshold if operator == "below" else value > threshold

    @staticmethod
    def _value(
        metrics: dict[str, Any], metric: str, class_index: int | None
    ) -> float | None:
        value = metrics.get(metric)
        if metric == "class_contribution":
            if value is None or class_index is None or class_index >= len(value):
                return None
            value = value[class_index]
        if value is None:
            return None
        return float(value)

    def _make_event(
        self,
        experiment_id: str,
        rule: CollapseRule,
        neuron_id: str,
        class_index: int | None,
        epoch: int,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        metrics = history[-1]["metrics"]
        affected_classes = (
            [class_index]
            if class_index is not None
            else list(rule.affected_classes)
        )
        event = {
            "experiment_id": experiment_id,
            "event_id": uuid4().hex,
            "neuron_id": neuron_id,
            "layer_id": metrics["layer_id"],
            "event_type": f"{rule.rule_id}_collapse"[:100],
            "affected_classes": affected_classes,
            "detected_epoch": epoch,
            "evidence": self.history_snapshot(history),
            "rule_version": rule.rule_id,
            "status": "active",
            "recovery": None,
        }
        return event
