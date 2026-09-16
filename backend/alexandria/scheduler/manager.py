from __future__ import annotations

import threading

from backend.alexandria.db.repository import Repository
from backend.alexandria.scheduler.runner import PipelineRunner


class Scheduler:
    def __init__(self, repository: Repository, runner: PipelineRunner):
        self.repo = repository
        self.runner = runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="alexandria-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self.repo.next_queued_job()
            if job_id:
                self.runner.run(job_id)
            else:
                self._stop.wait(0.5)
