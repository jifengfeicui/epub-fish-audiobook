"""Keep project conversations and their model context independent."""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if "agent_conversations" not in sa.inspect(connection).get_table_names():
        op.create_table("agent_conversations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("title", sa.String(100), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_agent_conversations_project_id", "agent_conversations", ["project_id"])
    if "conversation_id" not in {col["name"] for col in sa.inspect(connection).get_columns("agent_runs")}:
        # SQLite supports an additive nullable reference without rebuilding receipt tables.
        op.execute("ALTER TABLE agent_runs ADD COLUMN conversation_id VARCHAR(36) REFERENCES agent_conversations(id) ON DELETE CASCADE")
        op.create_index("ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"])
    op.execute("INSERT INTO agent_conversations (id, project_id, title, created_at) SELECT project_id, project_id, '历史对话', MIN(created_at) FROM agent_runs WHERE conversation_id IS NULL GROUP BY project_id")
    op.execute("UPDATE agent_runs SET conversation_id=project_id WHERE conversation_id IS NULL")


def downgrade():
    with op.batch_alter_table("agent_runs", naming_convention={"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}) as batch:
        batch.drop_index("ix_agent_runs_conversation_id")
        batch.drop_constraint("fk_agent_runs_conversation_id_agent_conversations", type_="foreignkey")
        batch.drop_column("conversation_id")
    op.drop_table("agent_conversations")
