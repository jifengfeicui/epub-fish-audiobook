"""Persist project script permissions and each agent run's authorization."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    for table, column in (("project_settings", "agent_auto_apply_scripts"), ("agent_runs", "auto_apply_scripts")):
        if column not in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table, sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("agent_runs", "auto_apply_scripts")
    op.drop_column("project_settings", "agent_auto_apply_scripts")
