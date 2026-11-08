"""JSON-safe metric conversion helpers."""

from __future__ import annotations

import math
from typing import Any


def json_number(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return numeric


def omit_missing(metrics: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metrics.items():
        if value is None:
            continue
        if isinstance(value, dict):
            result[key] = {
                inner_key: json_number(inner_value)
                for inner_key, inner_value in value.items()
            }
        elif isinstance(value, list):
            result[key] = [json_number(item) for item in value]
        else:
            result[key] = json_number(value) if isinstance(value, float) else value
    return result
