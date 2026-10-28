"""Project agent conversations, runs and pending actions."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_runs" not in tables:
        op.create_table(
            "agent_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("request_id", sa.String(36), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("selected_chapter_id", sa.Integer(), nullable=True),
            sa.Column("allow_text_rewrite", sa.Boolean(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("output", sa.Text(), nullable=False),
            sa.Column("history_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "request_id"),
        )
        op.create_index("ix_agent_runs_project_id", "agent_runs", ["project_id"])
        op.create_index("ix_agent_one_running", "agent_runs", ["project_id"], unique=True, sqlite_where=sa.text("status = 'running'"))
    if "agent_actions" not in tables:
        op.create_table(
            "agent_actions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("args_json", sa.Text(), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.UniqueConstraint("run_id"),
        )
        op.create_index("ix_agent_actions_run_id", "agent_actions", ["run_id"])


def downgrade():
    op.drop_table("agent_actions")
    op.drop_table("agent_runs")
