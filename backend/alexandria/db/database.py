from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Job, Project, StageRun


class Database:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(self.engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.sessions()

    def mark_running_interrupted(self) -> list[tuple[str, str]]:
        with self.session() as session, session.begin():
            jobs = session.execute(
                select(Job.id, Job.project_id).where(Job.status.in_(("running", "pausing")))
            ).all()
            project_ids = [project_id for _, project_id in jobs]
            session.execute(
                update(Job)
                .where(Job.status.in_(("running", "pausing")))
                .values(status="interrupted", error="Service restarted while the job was running")
            )
            session.execute(
                update(StageRun)
                .where(StageRun.status == "running")
                .values(status="interrupted", error="Service restarted during this stage")
            )
            if project_ids:
                session.execute(
                    update(Project)
                    .where(Project.id.in_(project_ids))
                    .values(status="interrupted")
                )
            return [(job_id, project_id) for job_id, project_id in jobs]
