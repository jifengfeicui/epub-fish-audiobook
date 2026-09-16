from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import WebSocket


@dataclass(eq=False)
class Connection:
    websocket: WebSocket
    ready: bool = False
    last_event_id: int = 0
    pending: list[dict] = field(default_factory=list)


class EventBroker:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connections: dict[str, dict[WebSocket, Connection]] = defaultdict(dict)
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, project_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        with self._lock:
            self._connections[project_id][websocket] = Connection(websocket)

    async def activate(self, project_id: str, websocket: WebSocket, after: int) -> None:
        while True:
            with self._lock:
                connection = self._connections.get(project_id, {}).get(websocket)
                if connection is None:
                    return
                pending = sorted(
                    (event for event in connection.pending if event.get("event_id", 0) > after),
                    key=lambda event: event["event_id"],
                )
                connection.pending.clear()
                if not pending:
                    connection.last_event_id = after
                    connection.ready = True
                    return
            for event in pending:
                await websocket.send_json(event)
                after = max(after, event["event_id"])
            with self._lock:
                connection = self._connections.get(project_id, {}).get(websocket)
                if connection:
                    connection.last_event_id = after

    def disconnect(self, project_id: str, websocket: WebSocket) -> None:
        with self._lock:
            self._connections[project_id].pop(websocket, None)

    def publish(self, project_id: str, event: dict) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(project_id, event), self._loop)

    async def _broadcast(self, project_id: str, event: dict) -> None:
        with self._lock:
            connections = list(self._connections.get(project_id, {}).values())
            targets = []
            for connection in connections:
                if connection.ready:
                    if event.get("event_id", 0) > connection.last_event_id:
                        connection.last_event_id = event["event_id"]
                        targets.append(connection.websocket)
                else:
                    connection.pending.append(event)
        stale: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_json(event)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(project_id, websocket)
