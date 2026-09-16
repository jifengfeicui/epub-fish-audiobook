"""Add archives and project character metadata.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    project_columns = {column["name"] for column in inspector.get_columns("projects")}
    if "archived_at" not in project_columns:
        op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    if "ix_projects_archived_at" not in {index["name"] for index in inspector.get_indexes("projects")}:
        op.create_index("ix_projects_archived_at", "projects", ["archived_at"])

    voice_columns = {column["name"] for column in inspector.get_columns("voice_profiles")}
    if "gender" not in voice_columns:
        op.add_column("voice_profiles", sa.Column("gender", sa.String(100), nullable=False, server_default=""))
    if "traits" not in voice_columns:
        op.add_column("voice_profiles", sa.Column("traits", sa.Text(), nullable=False, server_default=""))

    assignment_columns = {column["name"]: column for column in inspector.get_columns("speaker_assignments")}
    additions = {
        "gender": sa.Column("gender", sa.String(100), nullable=False, server_default=""),
        "personality": sa.Column("personality", sa.Text(), nullable=False, server_default=""),
        "line_count": sa.Column("line_count", sa.Integer(), nullable=False, server_default="0"),
        "importance": sa.Column("importance", sa.Integer(), nullable=False, server_default="0"),
        "first_seen": sa.Column("first_seen", sa.Integer(), nullable=False, server_default="0"),
        "user_edited": sa.Column("user_edited", sa.Boolean(), nullable=False, server_default=sa.false()),
    }
    legacy_assignments = not assignment_columns["voice_profile_id"]["nullable"] or any(
        name not in assignment_columns for name in additions
    )
    if legacy_assignments:
        with op.batch_alter_table("speaker_assignments") as batch:
            if not assignment_columns["voice_profile_id"]["nullable"]:
                batch.alter_column("voice_profile_id", existing_type=sa.String(36), nullable=True)
            for name, column in additions.items():
                if name not in assignment_columns:
                    batch.add_column(column)
        op.execute("DELETE FROM speaker_assignments")


def downgrade():
    op.execute("DELETE FROM speaker_assignments WHERE voice_profile_id IS NULL")
    with op.batch_alter_table("speaker_assignments") as batch:
        batch.drop_column("user_edited")
        batch.drop_column("first_seen")
        batch.drop_column("importance")
        batch.drop_column("line_count")
        batch.drop_column("personality")
        batch.drop_column("gender")
        batch.alter_column("voice_profile_id", existing_type=sa.String(36), nullable=False)
    op.drop_column("voice_profiles", "traits")
    op.drop_column("voice_profiles", "gender")
    op.drop_index("ix_projects_archived_at", table_name="projects")
    op.drop_column("projects", "archived_at")
