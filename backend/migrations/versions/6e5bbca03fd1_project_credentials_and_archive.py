"""Add named project credentials and reversible project archive."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6e5bbca03fd1"
down_revision: Union[str, Sequence[str], None] = "91dc874c2a61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.alter_column(
            "token_hash",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True)))

    with op.batch_alter_table("project_api_keys") as batch:
        batch.add_column(
            sa.Column(
                "name",
                sa.String(length=100),
                nullable=False,
                server_default="SDK authorization",
            )
        )


def downgrade() -> None:
    unkeyed_projects = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM projects WHERE token_hash IS NULL")
    ).scalar_one()
    if unkeyed_projects:
        raise RuntimeError(
            "Cannot downgrade while projects without legacy API keys exist; "
            "archive or migrate their credentials first."
        )

    with op.batch_alter_table("project_api_keys") as batch:
        batch.drop_column("name")

    with op.batch_alter_table("projects") as batch:
        batch.drop_column("deleted_at")
        batch.alter_column(
            "token_hash",
            existing_type=sa.String(length=64),
            nullable=False,
        )
