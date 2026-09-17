"""Move narrator binding from the global pool to each project.

Revision ID: 0004
Revises: 0003
"""
from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        INSERT INTO speaker_assignments
            (project_id, speaker, voice_profile_id, gender, personality, line_count, importance, first_seen, user_edited, created_at)
        SELECT p.id, 'NARRATOR', v.id, '', '', 0, 0, 0, 1, CURRENT_TIMESTAMP
        FROM projects p
        JOIN voice_profiles v ON v.bound_speaker = 'NARRATOR'
        WHERE NOT EXISTS (
            SELECT 1 FROM speaker_assignments s
            WHERE s.project_id = p.id AND s.speaker = 'NARRATOR'
        )
    """)
    op.execute("UPDATE voice_profiles SET bound_speaker = NULL")


def downgrade():
    op.execute("UPDATE voice_profiles SET bound_speaker = NULL")
    op.execute("""
        UPDATE voice_profiles SET bound_speaker = 'NARRATOR'
        WHERE id = (
            SELECT voice_profile_id FROM speaker_assignments
            WHERE speaker = 'NARRATOR' AND voice_profile_id IS NOT NULL
            ORDER BY created_at LIMIT 1
        )
    """)
    op.execute("DELETE FROM speaker_assignments WHERE speaker = 'NARRATOR'")
