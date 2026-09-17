"""Add voice availability, samples, and project exclusions.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("voice_profiles")}
    additions = {
        "enabled": sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        "sample_path": sa.Column("sample_path", sa.String(1000), nullable=True),
        "sample_reference_id": sa.Column("sample_reference_id", sa.String(100), nullable=True),
        "sample_title": sa.Column("sample_title", sa.String(500), nullable=True),
        "sample_text": sa.Column("sample_text", sa.Text(), nullable=True),
    }
    with op.batch_alter_table("voice_profiles") as batch:
        for name, column in additions.items():
            if name not in columns:
                batch.add_column(column)

    if "project_voice_exclusions" not in inspector.get_table_names():
        op.create_table(
            "project_voice_exclusions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("voice_profile_id", sa.String(36), sa.ForeignKey("voice_profiles.id", ondelete="CASCADE"), nullable=False),
            sa.UniqueConstraint("project_id", "voice_profile_id"),
        )
        op.create_index("ix_project_voice_exclusions_project_id", "project_voice_exclusions", ["project_id"])
        op.create_index("ix_project_voice_exclusions_voice_profile_id", "project_voice_exclusions", ["voice_profile_id"])


def downgrade():
    op.drop_table("project_voice_exclusions")
    with op.batch_alter_table("voice_profiles") as batch:
        batch.drop_column("sample_text")
        batch.drop_column("sample_title")
        batch.drop_column("sample_reference_id")
        batch.drop_column("sample_path")
        batch.drop_column("enabled")
