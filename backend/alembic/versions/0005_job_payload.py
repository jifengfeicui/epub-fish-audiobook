"""Persist job-specific payloads.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "payload_json" not in columns:
        op.add_column("jobs", sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "payload_json" in columns:
        op.drop_column("jobs", "payload_json")
