"""Ordered agent operations and persistent project automations."""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    if "project_automations" not in sa.inspect(op.get_bind()).get_table_names():
        create_automations()
    if "automation_id" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_runs")}:
        op.add_column("agent_runs", sa.Column("automation_id", sa.String(36), nullable=True))
    if "position" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_actions")}:
        with op.batch_alter_table("agent_actions", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
            batch.drop_constraint("uq_agent_actions_run_id", type_="unique")
            batch.add_column(sa.Column("position", sa.Integer(), nullable=False, server_default="0"))
            batch.create_unique_constraint("uq_agent_actions_run_position", ["run_id", "position"])


def create_automations():
    op.create_table(
        "project_automations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selected_chapter_id", sa.Integer(), nullable=True),
        sa.Column("selected_chapter_hash", sa.String(64), nullable=True),
        sa.Column("last_run_id", sa.String(36), nullable=True),
        sa.Column("acknowledged_run_id", sa.String(36), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_automations_project_id", "project_automations", ["project_id"])


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM agent_actions WHERE position > 0")):
        raise RuntimeError("Cannot downgrade while runs have multiple operations")
    constraints = sa.inspect(connection).get_unique_constraints("agent_actions")
    constraint = next(item for item in constraints if set(item["column_names"]) == {"run_id", "position"})
    with op.batch_alter_table("agent_actions", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
        batch.drop_constraint(constraint["name"] or "uq_agent_actions_run_id", type_="unique")
        batch.drop_column("position")
        batch.create_unique_constraint("uq_agent_actions_run_id", ["run_id"])
    op.drop_column("agent_runs", "automation_id")
    op.drop_table("project_automations")
