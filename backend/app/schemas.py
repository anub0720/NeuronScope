"""Backend request and response schemas."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _validate_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _validate_json_value(item)
        return value
    raise ValueError("Value must be JSON serializable")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_project_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be empty")
        return value


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=160)

    @field_validator("name")
    @classmethod
    def normalize_project_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Project name cannot be empty")
        return value


class ProjectCredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def normalize_credential_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Credential name cannot be empty")
        return value

class AccountRegister(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)


class AccountLogin(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class AccountEmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class AccountTokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class PasswordResetRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class DeviceAuthorizationCreate(BaseModel):
    project_id: str | None = Field(default=None, min_length=1, max_length=36)
    project_name: str = Field(default="NeuronScope", min_length=1, max_length=160)


class DeviceCodeRequest(BaseModel):
    device_code: str = Field(min_length=20, max_length=256)


class DeviceApprovalRequest(BaseModel):
    user_code: str = Field(min_length=6, max_length=16)


class ProjectClaimRequest(BaseModel):
    api_key: str = Field(min_length=20, max_length=256)


class ExperimentCreate(BaseModel):
    experiment_id: str | None = Field(default=None, min_length=1, max_length=36)
    name: str
    framework: str = "pytorch"
    model_name: str | None = None
    seed: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ArchitectureCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str | None = None
    version: int = Field(default=1, ge=1)
    framework: str = Field(default="pytorch", min_length=1, max_length=40)
    model_name: str = Field(min_length=1, max_length=200)
    classes: dict[str, str]
    layers: list["ArchitectureLayer"]

    @field_validator("classes")
    @classmethod
    def validate_class_indices(cls, classes: dict[str, str]) -> dict[str, str]:
        try:
            indices = sorted((int(index), index) for index in classes)
        except ValueError as exc:
            raise ValueError("class keys must be integer indices") from exc
        if [index for index, _ in indices] != list(range(len(indices))):
            raise ValueError("class keys must be contiguous indices starting at 0")
        if any(str(index) != key for index, key in indices):
            raise ValueError("class keys must use canonical integer strings")
        return classes

    @model_validator(mode="after")
    def validate_layers(self):
        ids = [layer.layer_id for layer in self.layers]
        if len(ids) != len(set(ids)):
            raise ValueError("layer IDs must be unique")
        neuron_ids = [
            neuron_id
            for layer in self.layers
            for neuron_id in (layer.neuron_ids or [])
        ]
        if len(neuron_ids) != len(set(neuron_ids)):
            raise ValueError("neuron IDs must be unique across the architecture")
        if not self.classes or not self.layers:
            raise ValueError("architecture requires at least one class and one layer")
        return self


class ArchitectureLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=100)
    neuron_count: int = Field(ge=1)
    neuron_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_neuron_ids(self):
        if self.neuron_ids is not None:
            if len(self.neuron_ids) != self.neuron_count:
                raise ValueError("neuron_ids must match neuron_count")
            if any(not neuron_id or len(neuron_id) > 200 for neuron_id in self.neuron_ids):
                raise ValueError("neuron IDs must contain 1 to 200 characters")
            if len(self.neuron_ids) != len(set(self.neuron_ids)):
                raise ValueError("neuron IDs must be unique within a layer")
        return self


class NeuronMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    layer_id: str = Field(min_length=1, max_length=200)
    activation_mean: float | None = None
    activation_variance: float | None = Field(default=None, ge=0)
    activation_quantiles: dict[str, float | None] | None = None
    inactive_fraction: float | None = Field(default=None, ge=0, le=1)
    gradient_mean: float | None = None
    gradient_variance: float | None = Field(default=None, ge=0)
    weight_norm: float | None = Field(default=None, ge=0)
    class_contribution: list[float | None] | None = None
    class_sample_counts: list[int | None] | None = None
    class_validity_mask: list[bool] | None = None
    class_contribution_uncertainty: list[float | None] | None = None
    sample_count: int | None = Field(default=None, ge=0)
    uncertainty: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_class_arrays(self):
        arrays = (
            self.class_contribution,
            self.class_sample_counts,
            self.class_validity_mask,
            self.class_contribution_uncertainty,
        )
        lengths = {len(array) for array in arrays if array is not None}
        if len(lengths) > 1:
            raise ValueError("class metric arrays must have matching lengths")
        return self

    @field_validator("class_contribution_uncertainty")
    @classmethod
    def validate_class_uncertainty(
        cls, values: list[float | None] | None
    ) -> list[float | None] | None:
        if values is not None and any(
            value is not None and value < 0 for value in values
        ):
            raise ValueError("class contribution uncertainty must be non-negative")
        return values

    @field_validator("class_sample_counts")
    @classmethod
    def validate_sample_counts(cls, counts: list[int | None] | None):
        if counts is not None and any(count is not None and count < 0 for count in counts):
            raise ValueError("class sample counts must be non-negative")
        return counts


class EpochCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    experiment_id: str | None = None
    epoch: int = Field(ge=0)
    neurons: dict[str, NeuronMetrics]

    @field_validator("neurons")
    @classmethod
    def validate_neuron_ids(cls, neurons: dict[str, NeuronMetrics]):
        if any(not neuron_id or len(neuron_id) > 200 for neuron_id in neurons):
            raise ValueError("neuron IDs must contain 1 to 200 characters")
        return neurons


class EpochChunkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    experiment_id: str | None = None
    chunk_index: int = Field(ge=0, le=9999)
    chunk_count: int = Field(ge=1, le=10000)
    neurons: dict[str, NeuronMetrics]

    @model_validator(mode="after")
    def validate_chunk_index(self):
        if self.chunk_index >= self.chunk_count:
            raise ValueError("chunk_index must be less than chunk_count")
        return self

    @field_validator("neurons")
    @classmethod
    def validate_neuron_ids(cls, neurons: dict[str, NeuronMetrics]):
        if any(not neuron_id or len(neuron_id) > 200 for neuron_id in neurons):
            raise ValueError("neuron IDs must contain 1 to 200 characters")
        if not neurons:
            raise ValueError("epoch chunks must contain at least one neuron")
        return neurons


class EpochCompleteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str | None = None
    chunk_count: int = Field(ge=1, le=10000)


class CollapseEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    experiment_id: str | None = None
    event_id: str = Field(min_length=1, max_length=128)
    neuron_id: str = Field(min_length=1, max_length=200)
    layer_id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=100)
    affected_classes: list[int] = Field(default_factory=list)
    detected_epoch: int = Field(ge=0)
    evidence: dict[str, Any] | None = None
    rule_version: str = Field(default="sdk", max_length=100)
    status: Literal["active", "recovered"] = "active"
    recovery: dict[str, Any] | None = None

    @field_validator("evidence", "recovery")
    @classmethod
    def validate_json_fields(cls, value: dict[str, Any] | None):
        if value is not None:
            _validate_json_value(value)
        return value

    @field_validator("recovery")
    @classmethod
    def validate_recovery_epoch(cls, recovery: dict[str, Any] | None):
        if recovery is not None and "epoch" in recovery:
            epoch = recovery["epoch"]
            if type(epoch) is not int or epoch < 0:
                raise ValueError("recovery epoch must be a non-negative integer")
        return recovery

    @field_validator("affected_classes")
    @classmethod
    def validate_affected_classes(cls, classes: list[int]):
        if any(index < 0 for index in classes):
            raise ValueError("affected class indices must be non-negative")
        if len(classes) != len(set(classes)):
            raise ValueError("affected class indices must be unique")
        return classes


class BatchObservation(BaseModel):
    epoch: int = Field(ge=0)
    layer_key: str
    unit_index: int | None = None
    class_key: str | None = None

    metric_name: str
    scope: Literal["model", "layer", "neuron", "neuron_class"]

    value: float | None = None
    sample_count: int | None = None
    std: float | None = None
    standard_error: float | None = None

    metadata_json: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.scope in ("neuron", "neuron_class") and self.unit_index is None:
            raise ValueError("unit_index required")

        if self.scope == "neuron_class" and self.class_key is None:
            raise ValueError("class_key required")

        if self.scope != "neuron_class" and self.class_key is not None:
            raise ValueError("class_key only allowed for neuron_class")

        return self


class BatchCreate(BaseModel):
    batch_id: str
    epoch_start: int
    epoch_end: int
    observations: list[BatchObservation]

    @model_validator(mode="after")
    def validate_epochs(self):
        if self.epoch_end < self.epoch_start:
            raise ValueError("Invalid epoch range")

        if any(
            not self.epoch_start <= obs.epoch <= self.epoch_end
            for obs in self.observations
        ):
            raise ValueError("Observation outside batch range")

        return self
