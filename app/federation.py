import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import httpx

from .database import Database
from .security import canonical_json, federation_signature, verify_federation_signature

logger = logging.getLogger("nexus.federation")


class Federation:
    def __init__(
        self,
        node_id: str,
        peers: list[str],
        secret: str,
        database: Database,
        on_remote_event: Callable[[dict[str, Any]], Awaitable[None]],
        on_snapshot_applied: Callable[[dict[str, int]], Awaitable[None]] | None = None,
    ) -> None:
        self.node_id = node_id
        self.peers = [peer.rstrip("/") for peer in peers if peer]
        self.secret = secret
        self.database = database
        self.on_remote_event = on_remote_event
        self.on_snapshot_applied = on_snapshot_applied
        self.client: httpx.AsyncClient | None = None
        self.worker: asyncio.Task[None] | None = None
        self.bootstrap_worker: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        # після зміни мережі недоставлені події перенапрямляються на актуальні адреси вузлів
        retargeted = await asyncio.to_thread(self.database.retarget_outbox, self.peers)
        if retargeted:
            logger.info(
                "Retargeted %s pending outbox item(s) to current peer URL(s): %s",
                retargeted,
                ", ".join(self.peers),
            )
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=2.0))
        self.worker = asyncio.create_task(self._outbox_worker(), name="federation-outbox")
        if self.peers and await asyncio.to_thread(self.database.needs_initial_replication):
            self.bootstrap_worker = asyncio.create_task(
                self._initial_replication_worker(), name="federation-initial-replication"
            )

    async def stop(self) -> None:
        self._stopping = True
        self._wakeup.set()
        if self.bootstrap_worker:
            self.bootstrap_worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.bootstrap_worker
        if self.worker:
            await self.worker
        if self.client:
            await self.client.aclose()

    async def publish(self, event: dict[str, Any], durable: bool = True) -> None:
        if durable:
            # стійкі події спочатку записуються в outbox, тому переживають відключення мережі й перезапуск
            for peer in self.peers:
                await asyncio.to_thread(self.database.enqueue, event["id"], peer, event)
            self._wakeup.set()
            return
        # аудіо й координати курсора надсилаються без запису в sqlite, бо вони швидко втрачають актуальність
        if self.peers:
            await asyncio.gather(*(self._send(peer, event) for peer in self.peers), return_exceptions=True)

    async def _send(self, peer: str, event: dict[str, Any]) -> bool:
        if not self.client:
            return False
        # підпис охоплює точні байти json і час, щоб вузол-одержувач виявив підміну або повтор
        body = canonical_json(event)
        timestamp = str(int(time.time()))
        signature = federation_signature(self.secret, timestamp, body)
        try:
            response = await self.client.post(
                f"{peer}/api/federation/events",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Nexus-Node": self.node_id,
                    "X-Nexus-Timestamp": timestamp,
                    "X-Nexus-Signature": signature,
                },
            )
            return response.status_code in (200, 201, 202, 409)
        except httpx.HTTPError as exc:
            logger.debug("Peer %s is unavailable: %s", peer, exc)
            return False

    async def _request_snapshot(self, peer: str) -> tuple[str, dict[str, Any]] | None:
        if not self.client:
            return None
        request_body = canonical_json({"requester": self.node_id, "version": 1})
        timestamp = str(int(time.time()))
        signature = federation_signature(self.secret, timestamp, request_body)
        try:
            response = await self.client.post(
                f"{peer}/api/federation/snapshot",
                content=request_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Nexus-Node": self.node_id,
                    "X-Nexus-Timestamp": timestamp,
                    "X-Nexus-Signature": signature,
                },
                timeout=30.0,
            )
            if response.status_code != 200 or len(response.content) > 128 * 1024 * 1024:
                return None
            source = response.headers.get("X-Nexus-Node", "")
            response_timestamp = response.headers.get("X-Nexus-Timestamp", "")
            response_signature = response.headers.get("X-Nexus-Signature", "")
            if not source or source == self.node_id or not verify_federation_signature(
                self.secret, response_timestamp, response.content, response_signature
            ):
                return None
            snapshot = response.json()
            if not isinstance(snapshot, dict) or snapshot.get("source_node") != source:
                return None
            if int(snapshot.get("version", 0)) != 1 or int(snapshot.get("record_count", -1)) < 0:
                return None
            return source, snapshot
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.debug("Cannot obtain initial snapshot from %s: %s", peer, exc)
            return None

    async def _initial_replication_worker(self) -> None:
        # новий вузол опитує всі налаштовані peer і обирає найновіший повний підписаний знімок
        await asyncio.sleep(0.5)
        while not self._stopping:
            results = await asyncio.gather(
                *(self._request_snapshot(peer) for peer in self.peers), return_exceptions=True
            )
            snapshots = [
                result for result in results
                if isinstance(result, tuple) and len(result) == 2
            ]
            non_empty = [item for item in snapshots if int(item[1].get("record_count", 0)) > 0]
            if non_empty:
                source, snapshot = max(
                    non_empty,
                    key=lambda item: (
                        int(item[1].get("max_lamport", 0)),
                        int(item[1].get("record_count", 0)),
                    ),
                )
                try:
                    stats = await asyncio.to_thread(self.database.import_snapshot, snapshot, source)
                    if self.on_snapshot_applied:
                        await self.on_snapshot_applied(stats)
                    logger.info(
                        "Initial replication imported %s record(s) from %s",
                        stats.get("records", 0),
                        source,
                    )
                    return
                except (ValueError, TypeError, KeyError) as exc:
                    logger.warning("Rejected initial snapshot from %s: %s", source, exc)
            if len(snapshots) == len(self.peers):
                # порожня мережа не ставить постійну позначку, щоб наступний запуск міг
                # виконати bootstrap, якщо до того часу на peer з'являться дані
                logger.info("Initial replication skipped: configured peers contain no user data")
                return
            await asyncio.sleep(2.0)

    async def _outbox_worker(self) -> None:
        # фонова задача дістає готові записи малими пакетами і видаляє їх лише після підтвердження іншого вузла
        while not self._stopping:
            rows = await asyncio.to_thread(self.database.due_outbox)
            for row in rows:
                try:
                    event = json.loads(row["payload"])
                    delivered = await self._send(row["peer"], event)
                    if delivered:
                        await asyncio.to_thread(
                            self.database.outbox_delivered, row["event_id"], row["peer"]
                        )
                    else:
                        await asyncio.to_thread(
                            self.database.outbox_failed,
                            row["event_id"],
                            row["peer"],
                            int(row["attempts"]) + 1,
                        )
                except Exception:
                    logger.exception("Cannot deliver federation event %s", row["event_id"])
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=1.0)
            except TimeoutError:
                pass
