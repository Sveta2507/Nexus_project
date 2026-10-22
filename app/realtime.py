import asyncio
import time
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        # один користувач може мати кілька одночасних websocket-з’єднань з різних вкладок
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._display_names: dict[str, str] = {}
        self._remote_online: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self, username: str, display_name: str, websocket: WebSocket, subprotocol: str | None = None
    ) -> None:
        await websocket.accept(subprotocol=subprotocol)
        async with self._lock:
            self._connections[username].add(websocket)
            self._display_names[username] = display_name

    async def disconnect(self, username: str, websocket: WebSocket) -> bool:
        async with self._lock:
            sockets = self._connections.get(username)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    self._connections.pop(username, None)
                    self._display_names.pop(username, None)
                    return True
            return False

    async def set_remote_presence(self, username: str, online: bool) -> None:
        async with self._lock:
            if online:
                # оренда статусу сама спливає, якщо інший вузол зник без повідомлення offline
                self._remote_online[username] = time.monotonic() + 30
            else:
                self._remote_online.pop(username, None)

    async def online_names(self) -> set[str]:
        async with self._lock:
            now = time.monotonic()
            expired = [username for username, deadline in self._remote_online.items() if deadline <= now]
            for username in expired:
                self._remote_online.pop(username, None)
            return set(self._connections) | set(self._remote_online)

    async def local_online(self, username: str) -> bool:
        async with self._lock:
            return bool(self._connections.get(username))

    async def broadcast(self, payload: dict[str, Any], recipients: list[str] | None = None) -> None:
        # список з’єднань копіюється під блокуванням, а мережеве очікування виконується вже без нього
        async with self._lock:
            if recipients is None:
                sockets = [socket for group in self._connections.values() for socket in group]
            else:
                sockets = [socket for name in set(recipients) for socket in self._connections.get(name, set())]
        if not sockets:
            return
        results = await asyncio.gather(*(socket.send_json(payload) for socket in sockets), return_exceptions=True)
        for socket, result in zip(sockets, results):
            if isinstance(result, Exception):
                # starlette видалить з’єднання під час обробки його відключення
                continue

    async def snapshot(self) -> list[dict[str, str]]:
        async with self._lock:
            return [
                {"username": username, "display_name": self._display_names.get(username, username)}
                for username in sorted(self._connections)
            ]
