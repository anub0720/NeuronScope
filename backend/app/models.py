"""Backend data models."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    String, Text, Integer, Float, Boolean, DateTime,
    ForeignKey, UniqueConstraint, Index, JSON
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def generate_id():
    return str(uuid.uuid4())


def utc_now():
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    experiments = relationship("Experiment", back_populates="project")
    owner = relationship("User", back_populates="projects")
    api_keys = relationship("ProjectAPIKey", back_populates="project")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    projects = relationship("Project", back_populates="owner")
    sessions = relationship("UserSession", back_populates="user")


class ProjectAPIKey(Base):
    __tablename__ = "project_api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, default="SDK authorization"
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project = relationship("Project", back_populates="api_keys")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user = relationship("User", back_populates="sessions")


class AccountToken(Base):
    __tablename__ = "account_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DeviceAuthorization(Base):
    __tablename__ = "device_authorizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    device_code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    requested_project_id: Mapped[str | None] = mapped_column(String(36))
    provisioned_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id")
    )
    project_name: Mapped[str] = mapped_column(String(160), nullable=False)
    approved_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    framework: Mapped[str] = mapped_column(String(40), default="pytorch")
    model_name: Mapped[str | None] = mapped_column(String(200))
    seed: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="running")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    latest_epoch: Mapped[int] = mapped_column(Integer, default=-1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project = relationship("Project", back_populates="experiments")
    architectures = relationship("Architecture", back_populates="experiment")
    classes = relationship("DatasetClass", back_populates="experiment")


class Architecture(Base):
    __tablename__ = "architectures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    model_signature: Mapped[str] = mapped_column(String(128))
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    experiment = relationship("Experiment", back_populates="architectures")
    layers = relationship("Layer", back_populates="architecture")

    __table_args__ = (
        UniqueConstraint("experiment_id", "version"),
    )


class Layer(Base):
    __tablename__ = "layers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    architecture_id: Mapped[str] = mapped_column(ForeignKey("architectures.id"), index=True)
    layer_key: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    module_type: Mapped[str] = mapped_column(String(100))
    unit_type: Mapped[str] = mapped_column(String(50))
    unit_count: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    architecture = relationship("Architecture", back_populates="layers")
    neurons = relationship("Neuron", back_populates="layer")

    __table_args__ = (
        UniqueConstraint("architecture_id", "layer_key"),
    )


class Neuron(Base):
    __tablename__ = "neurons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    layer_id: Mapped[str] = mapped_column(ForeignKey("layers.id"), index=True)
    unit_index: Mapped[int] = mapped_column(Integer)
    stable_key: Mapped[str] = mapped_column(String(300))
    unit_type: Mapped[str] = mapped_column(String(50), default="neuron")

    layer = relationship("Layer", back_populates="neurons")

    __table_args__ = (
        UniqueConstraint("layer_id", "unit_index"),
        UniqueConstraint("stable_key"),
    )


class DatasetClass(Base):
    __tablename__ = "dataset_classes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    class_key: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    label_encoding: Mapped[str | None] = mapped_column(String(100))

    experiment = relationship("Experiment", back_populates="classes")

    __table_args__ = (
        UniqueConstraint("experiment_id", "class_key"),
    )


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(30))
    unit: Mapped[str | None] = mapped_column(String(50))
    schema_version: Mapped[str] = mapped_column(String(20), default="0.1")


class MetricBatch(Base):
    __tablename__ = "metric_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    batch_id: Mapped[str] = mapped_column(String(128))
    epoch_start: Mapped[int] = mapped_column(Integer)
    epoch_end: Mapped[int] = mapped_column(Integer)
    row_count: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint("experiment_id", "batch_id"),
    )


class MetricObservation(Base):
    __tablename__ = "metric_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    epoch: Mapped[int] = mapped_column(Integer, index=True)

    layer_key: Mapped[str] = mapped_column(String(200), index=True)
    unit_index: Mapped[int | None] = mapped_column(Integer)
    class_key: Mapped[str | None] = mapped_column(String(200))

    metric_name: Mapped[str] = mapped_column(String(120), index=True)
    scope: Mapped[str] = mapped_column(String(30))

    value: Mapped[float | None] = mapped_column(Float)
    sample_count: Mapped[int | None] = mapped_column(Integer)
    std: Mapped[float | None] = mapped_column(Float)
    standard_error: Mapped[float | None] = mapped_column(Float)

    batch_id: Mapped[str] = mapped_column(String(128))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index(
            "ix_observation_lookup",
            "experiment_id", "epoch", "layer_key",
            "unit_index", "class_key", "metric_name"
        ),
    )


class BehaviorEvent(Base):
    __tablename__ = "behavior_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)

    layer_key: Mapped[str] = mapped_column(String(200))
    unit_index: Mapped[int] = mapped_column(Integer)
    class_key: Mapped[str | None] = mapped_column(String(200))

    event_type: Mapped[str] = mapped_column(String(60), index=True)
    first_observed_epoch: Mapped[int] = mapped_column(Integer)
    confirmation_epoch: Mapped[int | None] = mapped_column(Integer)
    end_epoch: Mapped[int | None] = mapped_column(Integer)

    rule_version: Mapped[str] = mapped_column(String(30))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
