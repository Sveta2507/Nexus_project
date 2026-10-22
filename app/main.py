import asyncio
import base64
import json
import logging
import math
import mimetypes
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .database import Database
from .federation import Federation
from .realtime import ConnectionManager
from .security import (
    canonical_json,
    federation_signature,
    hash_password,
    new_token,
    token_digest,
    verify_federation_signature,
    verify_password,
)

logger = logging.getLogger("nexus")
USERNAME_RE = re.compile(r"^[\w.-]{3,24}$", re.UNICODE)
ALLOWED_DOCUMENT_TYPES = {
    "text/plain",
    "text/markdown",
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}


# моделі pydantic обмежують розмір і форму вхідних даних ще до виконання бізнес-логіки
class AuthInput(BaseModel):
    username: str = Field(min_length=3, max_length=24)
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=50)


class MessageInput(BaseModel):
    body: str = Field(default="", max_length=4000)
    room_id: str = Field(default="lobby", max_length=64)
    encrypted: bool = False
    ciphertext: str | None = Field(default=None, max_length=12_000)
    nonce: str | None = Field(default=None, max_length=64)
    client_id: str | None = Field(default=None, max_length=80)
    client_created_at: float | None = None


class DeviceKeyInput(BaseModel):
    device_id: str = Field(min_length=16, max_length=80)
    public_key: dict[str, Any]


class KeyEnvelopeInput(BaseModel):
    recipient_device_id: str = Field(min_length=16, max_length=80)
    recipient_username: str = Field(min_length=3, max_length=24)
    sender_device_id: str = Field(min_length=16, max_length=80)
    sender_public_key: dict[str, Any]
    wrapped_key: str = Field(min_length=16, max_length=1000)
    nonce: str = Field(min_length=8, max_length=64)


class ChannelInput(BaseModel):
    id: str = Field(min_length=16, max_length=80)
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=160)
    member_usernames: list[str] = Field(default_factory=list, max_length=10)
    encrypted: bool = True
    key_envelopes: list[KeyEnvelopeInput] = Field(default_factory=list, max_length=50)


class LamportClock:
    def __init__(self) -> None:
        self.value = 0
        self.lock = threading.Lock()

    def tick(self, received: int | None = None) -> int:
        # після одержання віддаленої події локальний час стає більшим за обидва попередні значення
        with self.lock:
            self.value = max(self.value, received or 0) + 1
            return self.value


class Metrics:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.values: dict[str, int] = {
            "websocket_connections": 0,
            "messages_processed": 0,
            "audio_chunks": 0,
            "presentation_updates": 0,
            "federation_events": 0,
        }
        self.lock = threading.Lock()

    def increment(self, key: str, delta: int = 1) -> None:
        # блокування захищає лічильники від одночасної зміни різними потоками
        with self.lock:
            self.values[key] = self.values.get(key, 0) + delta

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {**self.values, "uptime_seconds": round(time.time() - self.started_at, 2)}


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "created_at": user["created_at"],
        "origin_node": user["origin_node"],
    }


def bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    raise HTTPException(status_code=401, detail="Потрібна авторизація")


def estimate_pdf_pages(content: bytes) -> int:
    # лічильник достатній для попереднього перегляду і не потребує окремої бібліотеки для pdf
    return max(1, len(re.findall(rb"/Type\s*/Page\b", content)))


def valid_base64(value: str, maximum_bytes: int) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) <= maximum_bytes
    except (ValueError, TypeError):
        return False


def client_message_id(value: Any) -> str:
    if value in (None, ""):
        return str(uuid.uuid4())
    return str(uuid.UUID(str(value)))


def client_message_timestamp(value: Any) -> float:
    now = time.time()
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return now
    if not math.isfinite(timestamp) or timestamp < now - 30 * 86400 or timestamp > now + 300:
        return now
    return timestamp


def public_device(device: dict[str, Any]) -> dict[str, Any]:
    result = device.copy()
    if isinstance(result.get("public_key"), str):
        result["public_key"] = json.loads(result["public_key"])
    return result


def create_app(settings: Settings) -> FastAPI:
    static_directory = Path(__file__).resolve().parent.parent / "static"
    database = Database(settings.database_path)
    manager = ConnectionManager()
    clock = LamportClock()
    metrics = Metrics()
    # мережевий цикл залишається асинхронним, а блокуючі виклики виконує обмежений пул потоків
    worker_count = max(4, min(16, (os.cpu_count() or 2) * 2))
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="nexus-worker")

    async def run_blocking(function, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, function, *args)

    async def make_event(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        # кожна розподілена подія має uuid, вузол-джерело і логічний час
        return {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "origin": settings.node_id,
            "lamport": clock.tick(),
            "created_at": time.time(),
            "data": data,
        }

    async def make_message_event(message: dict[str, Any]) -> dict[str, Any]:
        event = await make_event("message", message)
        event["id"] = f"message:{message['id']}"
        event["lamport"] = message["lamport"]
        return event

    async def make_receipt_event(receipt: dict[str, Any]) -> dict[str, Any]:
        status = "read" if receipt.get("read_at") is not None else "delivered"
        event = await make_event("message_receipt", receipt)
        event["id"] = f"receipt:{receipt['message_id']}:{receipt['device_id']}:{status}"
        return event

    async def users_payload() -> dict[str, Any]:
        users = await run_blocking(database.list_users)
        online = await manager.online_names()
        for user in users:
            user["online"] = user["username"] in online
        return {"type": "users", "users": users}

    async def broadcast_users() -> None:
        await manager.broadcast(await users_payload())

    async def notify_message_author(message_id: str) -> dict[str, Any]:
        summary = await run_blocking(database.message_receipt_summary, message_id)
        message = await run_blocking(database.get_message, message_id)
        if not message:
            return summary
        author = await run_blocking(database.user_by_id, message["author_id"])
        if author:
            await manager.broadcast(
                {
                    "type": "message_receipt",
                    "message_id": message_id,
                    "room_id": message["room_id"],
                    "summary": summary,
                },
                recipients=[author["username"]],
            )
        return summary

    async def apply_remote_event(event: dict[str, Any]) -> None:
        # усі події з іншого вузла проходять через одну точку перевірки, збереження і локальної розсилки
        event_type = event.get("type")
        data = event.get("data") or {}
        if not isinstance(data, dict):
            return
        received_lamport = int(event.get("lamport") or 0)
        clock.tick(received_lamport)
        metrics.increment("federation_events")

        # короткоживучі стани не записуються в таблицю оброблених подій
        ephemeral = event_type in {"audio_chunk", "presentation_state", "presence", "channel_key_request"}
        if not ephemeral:
            is_new = await run_blocking(database.mark_processed, str(event.get("id", "")))
            if not is_new:
                return

        if event_type == "user_upsert":
            await run_blocking(database.upsert_user, data)
            await broadcast_users()
        elif event_type == "message":
            inserted = await run_blocking(database.insert_message, data)
            if inserted:
                metrics.increment("messages_processed")
                recipients = await run_blocking(database.channel_member_usernames, data.get("room_id", "lobby"))
                await manager.broadcast({"type": "message", "message": data}, recipients=recipients)
        elif event_type == "message_receipt":
            try:
                receipt = {
                    "message_id": str(data["message_id"])[:80],
                    "device_id": str(data["device_id"])[:80],
                    "username": str(data["username"])[:24],
                    "delivered_at": float(data["delivered_at"]),
                    "read_at": float(data["read_at"]) if data.get("read_at") is not None else None,
                    "origin_node": str(data.get("origin_node") or event.get("origin") or "remote")[:80],
                }
            except (KeyError, TypeError, ValueError):
                return
            changed = await run_blocking(database.upsert_message_receipt, receipt)
            if changed:
                await notify_message_author(receipt["message_id"])
        elif event_type == "device_key":
            device = data.copy()
            if isinstance(device.get("public_key"), dict):
                device["public_key"] = json.dumps(device["public_key"], separators=(",", ":"), sort_keys=True)
            await run_blocking(database.upsert_device_key, device)
            await manager.broadcast({"type": "device_key_added", "device": public_device(device)})
        elif event_type == "channel":
            channel = data.get("channel") or {}
            members = data.get("members") or []
            envelopes = data.get("key_envelopes") or []
            normalized_envelopes = []
            for envelope in envelopes:
                item = envelope.copy()
                if isinstance(item.get("sender_public_key"), dict):
                    item["sender_public_key"] = json.dumps(
                        item["sender_public_key"], separators=(",", ":"), sort_keys=True
                    )
                normalized_envelopes.append(item)
            inserted = await run_blocking(
                database.insert_channel_bundle, channel, members, normalized_envelopes
            )
            if inserted:
                recipients = [member["username"] for member in members]
                channel_payload = {**channel, "members": members}
                await manager.broadcast(
                    {"type": "channel_added", "channel": channel_payload}, recipients=recipients
                )
                for envelope in normalized_envelopes:
                    await manager.broadcast(
                        {"type": "channel_key", "channel_id": channel["id"]},
                        recipients=[envelope["recipient_username"]],
                    )
        elif event_type == "channel_key":
            envelope = data.copy()
            if isinstance(envelope.get("sender_public_key"), dict):
                envelope["sender_public_key"] = json.dumps(
                    envelope["sender_public_key"], separators=(",", ":"), sort_keys=True
                )
            await run_blocking(database.insert_key_envelope, envelope)
            await manager.broadcast(
                {"type": "channel_key", "channel_id": envelope["channel_id"]},
                recipients=[envelope["recipient_username"]],
            )
        elif event_type == "channel_key_request":
            channel_id = str(data.get("channel_id", ""))
            recipients = await run_blocking(database.channel_member_usernames, channel_id)
            if recipients:
                await manager.broadcast(
                    {"type": "channel_key_request", **data}, recipients=recipients
                )
        elif event_type == "document":
            document = data.copy()
            encoded_content = document.pop("content_base64", "")
            document["content"] = base64.b64decode(encoded_content)
            document.setdefault("channel_id", "lobby")
            inserted = await run_blocking(database.insert_document, document)
            if inserted:
                document.pop("content", None)
                recipients = await run_blocking(database.channel_member_usernames, document["channel_id"])
                await manager.broadcast(
                    {"type": "document_added", "document": document}, recipients=recipients
                )
        elif event_type == "document_deleted":
            document = await run_blocking(database.get_document, data["id"], False)
            await run_blocking(database.delete_document, data["id"], data["owner_id"])
            channel_id = str(data.get("channel_id") or (document or {}).get("channel_id") or "lobby")
            recipients = await run_blocking(database.channel_member_usernames, channel_id)
            await manager.broadcast(
                {"type": "document_deleted", "document_id": data["id"], "channel_id": channel_id},
                recipients=recipients,
            )
        elif event_type in {"audio_chunk", "presentation_state"}:
            raw_recipients = data.get("recipients", [])
            recipients = [str(item) for item in raw_recipients][:10] if isinstance(raw_recipients, list) else []
            await manager.broadcast({"type": event_type, **data}, recipients=recipients)
        elif event_type == "presence":
            await manager.set_remote_presence(str(data.get("username", "")), bool(data.get("online")))
            await broadcast_users()

    async def snapshot_applied(stats: dict[str, int]) -> None:
        imported_lamport = int(stats.get("max_lamport", 0))
        if imported_lamport:
            clock.tick(imported_lamport)
        await broadcast_users()

    federation = Federation(
        settings.node_id,
        settings.peers,
        settings.federation_secret,
        database,
        apply_remote_event,
        snapshot_applied,
    )

    async def presence_heartbeat() -> None:
        # періодичний сигнал продовжує оренду online-статусу на інших вузлах
        while True:
            await asyncio.sleep(10)
            local_users = await manager.snapshot()
            if local_users:
                events = [
                    await make_event(
                        "presence",
                        {"username": user["username"], "display_name": user["display_name"], "online": True},
                    )
                    for user in local_users
                ]
                await asyncio.gather(
                    *(federation.publish(event, durable=False) for event in events),
                    return_exceptions=True,
                )
            # повторна розсилка прибирає з інтерфейсу статуси, чия оренда вже спливла
            await broadcast_users()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # життєвий цикл запускає базу, федерацію і heartbeat, а під час зупинки коректно завершує кожен ресурс
        await run_blocking(database.initialize)
        existing_lamport = await run_blocking(database.max_lamport)
        if existing_lamport:
            clock.tick(existing_lamport)
        await federation.start()
        heartbeat_task = asyncio.create_task(presence_heartbeat(), name="presence-heartbeat")
        logger.info(
            "Nexus node %s started on port %s with %s peer(s), database=%s",
            settings.node_id,
            settings.port,
            len(settings.peers),
            settings.database_path,
        )
        try:
            yield
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            await federation.stop()
            executor.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(
        title="Nexus Distributed Collaboration API",
        version="1.0.0",
        description="Багатопотокова розподілена система комунікації, демонстрації документів та аудіостримінгу.",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.manager = manager

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # заголовки обмежують джерела скриптів, вбудовування сторінки та непотрібні дозволи пристрою
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        if request.url.path.endswith(".js"):
            response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        elif request.url.path.endswith(".css"):
            response.headers["Content-Type"] = "text/css; charset=utf-8"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws: wss:; img-src 'self' blob: data:; media-src 'self' blob:; "
            "frame-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def current_user(request: Request) -> dict[str, Any]:
        token = bearer_token(request)
        user = await run_blocking(database.user_by_session, token_digest(token))
        if not user:
            raise HTTPException(status_code=401, detail="Сеанс завершився або є недійсним")
        return user

    async def create_login(user: dict[str, Any]) -> dict[str, Any]:
        # клієнт отримує випадковий токен, а сервер зберігає лише його хеш і строк дії
        token = new_token()
        await run_blocking(
            database.create_session,
            token_digest(token),
            user["id"],
            time.time() + settings.session_hours * 3600,
        )
        return {"token": token, "user": public_user(user), "node_id": settings.node_id}

    @app.post("/api/auth/register", status_code=201)
    async def register(data: AuthInput) -> dict[str, Any]:
        username = data.username.strip().lower()
        display_name = (data.display_name or data.username).strip()
        if not USERNAME_RE.fullmatch(username):
            raise HTTPException(status_code=422, detail="Логін: 3–24 літери, цифри, '.', '_' або '-'")
        if not display_name:
            raise HTTPException(status_code=422, detail="Вкажіть ім’я")
        if await run_blocking(database.user_by_username, username):
            raise HTTPException(status_code=409, detail="Такий логін уже використовується")
        try:
            salt, password_hash = await run_blocking(hash_password, data.password)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        user = {
            "id": str(uuid.uuid4()),
            "username": username,
            "display_name": display_name[:50],
            "password_salt": salt,
            "password_hash": password_hash,
            "created_at": time.time(),
            "origin_node": settings.node_id,
        }
        try:
            await run_blocking(database.create_user, user)
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Не вдалося створити користувача") from exc
        event = await make_event("user_upsert", user.copy())
        await federation.publish(event, durable=True)
        await broadcast_users()
        return await create_login(user)

    @app.post("/api/auth/login")
    async def login(data: AuthInput) -> dict[str, Any]:
        user = await run_blocking(database.user_by_username, data.username.strip().lower())
        valid = user and await run_blocking(
            verify_password, data.password, user["password_salt"], user["password_hash"]
        )
        if not valid:
            raise HTTPException(status_code=401, detail="Неправильний логін або пароль")
        return await create_login(user)

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request, _: dict[str, Any] = Depends(current_user)) -> Response:
        await run_blocking(database.delete_session, token_digest(bearer_token(request)))
        return Response(status_code=204)

    @app.get("/api/me")
    async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return {"user": public_user(user), "node_id": settings.node_id}

    @app.get("/api/users")
    async def users(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return await users_payload()

    @app.get("/api/keys/devices")
    async def device_keys(
        usernames: str = "", _: dict[str, Any] = Depends(current_user)
    ) -> dict[str, Any]:
        requested = [item.strip().lower() for item in usernames.split(",") if item.strip()][:10]
        devices = await run_blocking(database.list_device_keys, requested or None)
        return {"devices": [public_device(device) for device in devices]}

    @app.post("/api/keys/devices", status_code=201)
    async def register_device_key(
        data: DeviceKeyInput, user: dict[str, Any] = Depends(current_user)
    ) -> dict[str, Any]:
        try:
            uuid.UUID(data.device_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Некоректний ідентифікатор пристрою") from exc
        if (
            data.public_key.get("kty") != "EC"
            or data.public_key.get("crv") != "P-256"
            or not isinstance(data.public_key.get("x"), str)
            or not isinstance(data.public_key.get("y"), str)
            or not 40 <= len(data.public_key["x"]) <= 50
            or not 40 <= len(data.public_key["y"]) <= 50
        ):
            raise HTTPException(status_code=422, detail="Підтримується лише ключ ECDH P-256")
        existing = await run_blocking(database.device_key, data.device_id)
        if existing and existing["username"] != user["username"]:
            raise HTTPException(status_code=409, detail="Ідентифікатор пристрою вже зайнятий")
        device = {
            "device_id": data.device_id,
            "user_id": user["id"],
            "username": user["username"],
            "public_key": json.dumps(data.public_key, separators=(",", ":"), sort_keys=True),
            "created_at": time.time(),
            "origin_node": settings.node_id,
        }
        await run_blocking(database.upsert_device_key, device)
        public = public_device(device)
        await manager.broadcast({"type": "device_key_added", "device": public})
        await federation.publish(await make_event("device_key", public), durable=True)
        return {"device": public}

    @app.get("/api/channels")
    async def channels(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return {"channels": await run_blocking(database.list_channels_for_user, user["username"])}

    @app.post("/api/channels", status_code=201)
    async def create_channel(
        data: ChannelInput, user: dict[str, Any] = Depends(current_user)
    ) -> dict[str, Any]:
        # приватний канал приймає окремо загорнутий ключ для кожного зареєстрованого пристрою учасника
        try:
            uuid.UUID(data.id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Некоректний ідентифікатор каналу") from exc
        if await run_blocking(database.get_channel, data.id):
            raise HTTPException(status_code=409, detail="Канал уже існує")
        usernames = list(dict.fromkeys([user["username"], *(name.lower() for name in data.member_usernames)]))
        if len(usernames) > 10:
            raise HTTPException(status_code=422, detail="Канал підтримує до 10 учасників")
        known_users = {item["username"]: item for item in await run_blocking(database.list_users)}
        missing_users = [name for name in usernames if name not in known_users]
        if missing_users:
            raise HTTPException(status_code=422, detail=f"Невідомі користувачі: {', '.join(missing_users)}")
        channel = {
            "id": data.id,
            "name": data.name.strip(),
            "description": data.description.strip(),
            "owner_id": user["id"],
            "owner_name": user["display_name"],
            "is_private": 1,
            "encrypted": int(data.encrypted),
            "created_at": time.time(),
            "origin_node": settings.node_id,
        }
        if not channel["name"]:
            raise HTTPException(status_code=422, detail="Назва каналу порожня")
        members = [
            {
                "channel_id": data.id,
                "user_id": known_users[name]["id"],
                "username": name,
                "role": "owner" if name == user["username"] else "member",
            }
            for name in usernames
        ]
        envelopes: list[dict[str, Any]] = []
        if data.encrypted:
            devices = await run_blocking(database.list_device_keys, usernames)
            devices_by_id = {device["device_id"]: device for device in devices}
            missing_device_users = [name for name in usernames if not any(d["username"] == name for d in devices)]
            if missing_device_users:
                raise HTTPException(
                    status_code=422,
                    detail=f"Спочатку користувачі мають увійти хоча б раз: {', '.join(missing_device_users)}",
                )
            supplied_ids = {item.recipient_device_id for item in data.key_envelopes}
            required_ids = set(devices_by_id)
            if supplied_ids != required_ids:
                raise HTTPException(status_code=422, detail="Немає E2EE-ключа для кожного пристрою учасників")
            for item in data.key_envelopes:
                target = devices_by_id.get(item.recipient_device_id)
                sender = devices_by_id.get(item.sender_device_id)
                if (
                    not target
                    or target["username"] != item.recipient_username.lower()
                    or not sender
                    or sender["username"] != user["username"]
                    or json.loads(sender["public_key"]).get("x") != item.sender_public_key.get("x")
                    or json.loads(sender["public_key"]).get("y") != item.sender_public_key.get("y")
                    or not valid_base64(item.wrapped_key, 128)
                    or not valid_base64(item.nonce, 32)
                ):
                    raise HTTPException(status_code=422, detail="Некоректний конверт ключа каналу")
                envelopes.append(
                    {
                        "channel_id": data.id,
                        "recipient_device_id": item.recipient_device_id,
                        "recipient_username": item.recipient_username.lower(),
                        "sender_device_id": item.sender_device_id,
                        "sender_public_key": json.dumps(
                            item.sender_public_key, separators=(",", ":"), sort_keys=True
                        ),
                        "wrapped_key": item.wrapped_key,
                        "nonce": item.nonce,
                        "created_at": time.time(),
                    }
                )
        await run_blocking(database.insert_channel_bundle, channel, members, envelopes)
        public_channel = {**channel, "members": members}
        recipients = [member["username"] for member in members]
        await manager.broadcast({"type": "channel_added", "channel": public_channel}, recipients=recipients)
        replicated_envelopes = [
            {**item, "sender_public_key": json.loads(item["sender_public_key"])} for item in envelopes
        ]
        await federation.publish(
            await make_event(
                "channel",
                {"channel": channel, "members": members, "key_envelopes": replicated_envelopes},
            ),
            durable=True,
        )
        return {"channel": public_channel}

    @app.get("/api/channels/{channel_id}/key")
    async def channel_key(
        channel_id: str,
        device_id: str,
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        device = await run_blocking(database.device_key, device_id)
        if not device or device["username"] != user["username"]:
            raise HTTPException(status_code=403, detail="Пристрій не належить користувачу")
        if not await run_blocking(database.user_can_access_channel, channel_id, user["username"]):
            raise HTTPException(status_code=403, detail="Немає доступу до каналу")
        envelope = await run_blocking(database.key_envelope, channel_id, device_id, user["username"])
        if not envelope:
            raise HTTPException(status_code=404, detail="Ключ для цього пристрою ще не надано")
        envelope["sender_public_key"] = json.loads(envelope["sender_public_key"])
        return {"key_envelope": envelope}

    @app.post("/api/channels/{channel_id}/keys", status_code=201)
    async def add_channel_key(
        channel_id: str,
        data: KeyEnvelopeInput,
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, bool]:
        if not await run_blocking(database.user_can_access_channel, channel_id, user["username"]):
            raise HTTPException(status_code=403, detail="Немає доступу до каналу")
        sender = await run_blocking(database.device_key, data.sender_device_id)
        target = await run_blocking(database.device_key, data.recipient_device_id)
        member_names = await run_blocking(database.channel_member_usernames, channel_id)
        if (
            not sender
            or sender["username"] != user["username"]
            or not target
            or target["username"] != data.recipient_username.lower()
            or json.loads(sender["public_key"]).get("x") != data.sender_public_key.get("x")
            or json.loads(sender["public_key"]).get("y") != data.sender_public_key.get("y")
            or member_names is None
            or target["username"] not in member_names
            or not valid_base64(data.wrapped_key, 128)
            or not valid_base64(data.nonce, 32)
        ):
            raise HTTPException(status_code=422, detail="Некоректний конверт ключа")
        envelope = {
            "channel_id": channel_id,
            "recipient_device_id": data.recipient_device_id,
            "recipient_username": data.recipient_username.lower(),
            "sender_device_id": data.sender_device_id,
            "sender_public_key": json.dumps(data.sender_public_key, separators=(",", ":"), sort_keys=True),
            "wrapped_key": data.wrapped_key,
            "nonce": data.nonce,
            "created_at": time.time(),
        }
        await run_blocking(database.insert_key_envelope, envelope)
        await manager.broadcast(
            {"type": "channel_key", "channel_id": channel_id}, recipients=[envelope["recipient_username"]]
        )
        replicated = {**envelope, "sender_public_key": data.sender_public_key}
        await federation.publish(await make_event("channel_key", replicated), durable=True)
        return {"stored": True}

    @app.get("/api/messages")
    async def messages(
        room_id: str = "lobby", limit: int = 100, user: dict[str, Any] = Depends(current_user)
    ) -> dict[str, Any]:
        if not await run_blocking(database.user_can_access_channel, room_id, user["username"]):
            raise HTTPException(status_code=403, detail="Немає доступу до каналу")
        return {"messages": await run_blocking(database.list_messages, room_id, limit)}

    @app.post("/api/messages", status_code=201)
    async def post_message(data: MessageInput, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        # цей rest-маршрут і websocket нижче застосовують однакові правила доступу, шифрування і дедуплікації
        channel = await run_blocking(database.get_channel, data.room_id)
        if not channel or not await run_blocking(
            database.user_can_access_channel, data.room_id, user["username"]
        ):
            raise HTTPException(status_code=403, detail="Немає доступу до каналу")
        channel_encrypted = bool(channel["encrypted"])
        if channel_encrypted:
            if (
                not data.encrypted
                or not data.ciphertext
                or not data.nonce
                or not valid_base64(data.ciphertext, 8192)
                or not valid_base64(data.nonce, 32)
            ):
                raise HTTPException(status_code=422, detail="Зашифрований канал приймає лише E2EE-повідомлення")
            body = ""
        else:
            body = data.body.strip()
            if not body:
                raise HTTPException(status_code=422, detail="Повідомлення порожнє")
        try:
            message_id = client_message_id(data.client_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Некоректний client_id повідомлення") from exc
        message = {
            "id": message_id,
            "room_id": data.room_id,
            "author_id": user["id"],
            "author_name": user["display_name"],
            "body": body,
            "encrypted": int(channel_encrypted),
            "ciphertext": data.ciphertext if channel_encrypted else None,
            "nonce": data.nonce if channel_encrypted else None,
            "created_at": client_message_timestamp(data.client_created_at),
            "lamport": clock.tick(),
            "origin_node": settings.node_id,
        }
        inserted = await run_blocking(database.insert_message, message)
        if not inserted:
            existing = await run_blocking(database.get_message, message_id)
            if existing and existing["author_id"] == user["id"] and existing["room_id"] == data.room_id:
                await federation.publish(await make_message_event(existing), durable=True)
                return {"message": existing, "duplicate": True}
            raise HTTPException(status_code=409, detail="Ідентифікатор повідомлення вже використовується")
        metrics.increment("messages_processed")
        recipients = await run_blocking(database.channel_member_usernames, data.room_id)
        await federation.publish(await make_message_event(message), durable=True)
        await manager.broadcast({"type": "message", "message": message}, recipients=recipients)
        return {"message": message}

    @app.get("/api/documents")
    async def documents(
        channel_id: str = "lobby", user: dict[str, Any] = Depends(current_user)
    ) -> dict[str, Any]:
        channel_id = channel_id[:80]
        if not await run_blocking(database.user_can_access_channel, channel_id, user["username"]):
            raise HTTPException(status_code=403, detail="Немає доступу до каналу")
        return {
            "documents": await run_blocking(database.list_documents, channel_id, user["username"])
        }

    @app.post("/api/documents", status_code=201)
    async def upload_document(
        file: UploadFile = File(...),
        channel_id: str = Form("lobby"),
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        # файл прив’язується до каналу, а доступ до його вмісту повторно перевіряється при кожному завантаженні
        channel_id = channel_id[:80]
        if not await run_blocking(database.user_can_access_channel, channel_id, user["username"]):
            raise HTTPException(status_code=403, detail="Немає доступу до каналу")
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Максимальний розмір документа — 8 МБ")
        media_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
        if media_type not in ALLOWED_DOCUMENT_TYPES:
            raise HTTPException(status_code=415, detail="Підтримуються TXT, MD, PDF та зображення")
        kind = "slides" if media_type == "application/pdf" else "text"
        page_count = 1
        if media_type == "application/pdf":
            page_count = await run_blocking(estimate_pdf_pages, content)
        document = {
            "id": str(uuid.uuid4()),
            "channel_id": channel_id,
            "owner_id": user["id"],
            "owner_name": user["display_name"],
            "name": (file.filename or "document")[:180],
            "media_type": media_type,
            "kind": kind,
            "page_count": min(max(page_count, 1), 1000),
            "content": content,
            "created_at": time.time(),
            "origin_node": settings.node_id,
        }
        await run_blocking(database.insert_document, document)
        public_document = {key: value for key, value in document.items() if key != "content"}
        public_document["size"] = len(content)
        recipients = await run_blocking(database.channel_member_usernames, channel_id)
        await manager.broadcast(
            {"type": "document_added", "document": public_document}, recipients=recipients
        )
        replicated = {**public_document, "content_base64": base64.b64encode(content).decode("ascii")}
        await federation.publish(await make_event("document", replicated), durable=True)
        return {"document": public_document}

    @app.get("/api/documents/{document_id}/content")
    async def document_content(request: Request, document_id: str) -> Response:
        authorization = request.headers.get("Authorization", "")
        auth_token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        user = await run_blocking(database.user_by_session, token_digest(auth_token or ""))
        if not user:
            raise HTTPException(status_code=401, detail="Потрібна авторизація")
        document = await run_blocking(database.get_document, document_id, True)
        if not document:
            raise HTTPException(status_code=404, detail="Документ не знайдено")
        if not await run_blocking(database.user_can_access_document, document_id, user["username"]):
            raise HTTPException(status_code=403, detail="Немає доступу до документа")
        safe_name = quote(document["name"])
        return Response(
            content=document["content"],
            media_type=document["media_type"],
            headers={"Content-Disposition": f"inline; filename*=UTF-8''{safe_name}"},
        )

    @app.delete("/api/documents/{document_id}", status_code=204)
    async def delete_document(document_id: str, user: dict[str, Any] = Depends(current_user)) -> Response:
        document = await run_blocking(database.get_document, document_id, False)
        if not document or document["owner_id"] != user["id"]:
            raise HTTPException(
                status_code=404,
                detail="Документ не знайдено або він належить іншому користувачу",
            )
        deleted = await run_blocking(database.delete_document, document_id, user["id"])
        if not deleted:
            raise HTTPException(status_code=404, detail="Документ не знайдено або він належить іншому користувачу")
        recipients = await run_blocking(database.channel_member_usernames, document["channel_id"])
        await manager.broadcast(
            {
                "type": "document_deleted",
                "document_id": document_id,
                "channel_id": document["channel_id"],
            },
            recipients=recipients,
        )
        await federation.publish(
            await make_event(
                "document_deleted",
                {"id": document_id, "owner_id": user["id"], "channel_id": document["channel_id"]},
            ),
            durable=True,
        )
        return Response(status_code=204)

    @app.post("/api/federation/events", status_code=202)
    async def federation_event(request: Request) -> dict[str, bool]:
        # міжвузловий маршрут не довіряє тілу запиту, доки не перевірено hmac-підпис і час
        body = await request.body()
        timestamp = request.headers.get("X-Nexus-Timestamp", "")
        signature = request.headers.get("X-Nexus-Signature", "")
        source_node = request.headers.get("X-Nexus-Node", "")
        if not source_node or not verify_federation_signature(
            settings.federation_secret, timestamp, body, signature
        ):
            raise HTTPException(status_code=401, detail="Недійсний підпис міжвузлового запиту")
        try:
            import json

            event = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Некоректна подія") from exc
        if event.get("origin") != source_node:
            raise HTTPException(status_code=400, detail="Вузол-джерело не збігається")
        await apply_remote_event(event)
        return {"accepted": True}

    @app.post("/api/federation/snapshot")
    async def federation_snapshot(request: Request) -> Response:
        # повний знімок доступний лише вузлу зі спільним federation secret
        body = await request.body()
        timestamp = request.headers.get("X-Nexus-Timestamp", "")
        signature = request.headers.get("X-Nexus-Signature", "")
        source_node = request.headers.get("X-Nexus-Node", "")
        if not source_node or not verify_federation_signature(
            settings.federation_secret, timestamp, body, signature
        ):
            raise HTTPException(status_code=401, detail="Недійсний підпис запиту знімка")
        try:
            request_data = json.loads(body)
            protocol_version = int(request_data.get("version", 0))
        except (ValueError, TypeError, UnicodeDecodeError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail="Некоректний запит знімка") from exc
        if request_data.get("requester") != source_node or protocol_version != 1:
            raise HTTPException(status_code=400, detail="Некоректні параметри початкової реплікації")
        snapshot = await run_blocking(database.export_snapshot)
        snapshot["source_node"] = settings.node_id
        snapshot["max_lamport"] = max(int(snapshot.get("max_lamport", 0)), clock.value)
        response_body = canonical_json(snapshot)
        response_timestamp = str(int(time.time()))
        response_signature = federation_signature(
            settings.federation_secret, response_timestamp, response_body
        )
        return Response(
            content=response_body,
            media_type="application/json",
            headers={
                "X-Nexus-Node": settings.node_id,
                "X-Nexus-Timestamp": response_timestamp,
                "X-Nexus-Signature": response_signature,
            },
        )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "node_id": settings.node_id, "peers": settings.peers}

    @app.get("/api/system")
    async def system(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return {
            "node_id": settings.node_id,
            "process_id": os.getpid(),
            "thread_count": threading.active_count(),
            "worker_threads": worker_count,
            "lamport_clock": clock.value,
            "peers": settings.peers,
            "database": await run_blocking(database.counts),
            "metrics": metrics.snapshot(),
        }

    async def websocket_user(websocket: WebSocket) -> dict[str, Any] | None:
        offered_protocols = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
        protocol_token = next((item[5:] for item in offered_protocols if item.startswith("auth.")), "")
        return await run_blocking(database.user_by_session, token_digest(protocol_token))

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        # токен передається як websocket-підпроток, тому не потрапляє в url і журнали проксі
        user = await websocket_user(websocket)
        if not user:
            await websocket.close(code=4401, reason="Unauthorized")
            return
        username = user["username"]
        first_local_connection = not await manager.local_online(username)
        offered_protocols = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
        await manager.connect(
            username,
            user["display_name"],
            websocket,
            subprotocol="nexus" if "nexus" in offered_protocols else None,
        )
        metrics.increment("websocket_connections")
        await websocket.send_json(
            {"type": "hello", "node_id": settings.node_id, "user": public_user(user), "lamport": clock.value}
        )
        await broadcast_users()
        if first_local_connection:
            await federation.publish(
                await make_event(
                    "presence",
                    {"username": username, "display_name": user["display_name"], "online": True},
                ),
                durable=False,
            )
        try:
            # одне довгоживуче з’єднання передає чат, підтвердження, голос і стан демонстрації
            while True:
                payload = await websocket.receive_json()
                event_type = payload.get("type")
                if event_type == "ping":
                    await websocket.send_json({"type": "pong", "at": time.time()})
                elif event_type == "message_send":
                    raw_client_id = payload.get("client_id")

                    async def reject_message(reason: str) -> None:
                        await websocket.send_json(
                            {
                                "type": "message_rejected" if raw_client_id else "error",
                                "client_id": str(raw_client_id or ""),
                                "message": reason,
                            }
                        )

                    try:
                        message_id = client_message_id(raw_client_id)
                    except ValueError:
                        await reject_message("Некоректний client_id повідомлення")
                        continue
                    channel_id = str(payload.get("channel_id", "lobby"))[:80]
                    channel = await run_blocking(database.get_channel, channel_id)
                    can_access = channel and await run_blocking(
                        database.user_can_access_channel, channel_id, username
                    )
                    if not can_access:
                        await reject_message("Немає доступу до каналу")
                        continue
                    channel_encrypted = bool(channel["encrypted"])
                    if channel_encrypted:
                        ciphertext = payload.get("ciphertext")
                        nonce = payload.get("nonce")
                        if (
                            payload.get("encrypted") is not True
                            or not isinstance(ciphertext, str)
                            or not isinstance(nonce, str)
                            or not valid_base64(ciphertext, 8192)
                            or not valid_base64(nonce, 32)
                        ):
                            await reject_message("Канал вимагає коректне E2EE-повідомлення")
                            continue
                        body = ""
                    else:
                        raw_body = payload.get("body", "")
                        body = raw_body.strip() if isinstance(raw_body, str) else ""
                        ciphertext = None
                        nonce = None
                        if not body or len(body) > 4000:
                            await reject_message("Повідомлення порожнє або завелике")
                            continue
                    message = {
                        "id": message_id,
                        "room_id": channel_id,
                        "author_id": user["id"],
                        "author_name": user["display_name"],
                        "body": body,
                        "encrypted": int(channel_encrypted),
                        "ciphertext": ciphertext,
                        "nonce": nonce,
                        "created_at": client_message_timestamp(payload.get("client_created_at")),
                        "lamport": clock.tick(),
                        "origin_node": settings.node_id,
                    }
                    inserted = await run_blocking(database.insert_message, message)
                    if not inserted:
                        existing = await run_blocking(database.get_message, message_id)
                        if existing and existing["author_id"] == user["id"] and existing["room_id"] == channel_id:
                            await federation.publish(await make_message_event(existing), durable=True)
                            await websocket.send_json(
                                {
                                    "type": "message_ack",
                                    "client_id": message_id,
                                    "message_id": message_id,
                                    "room_id": channel_id,
                                    "duplicate": True,
                                }
                            )
                        else:
                            await reject_message("Ідентифікатор повідомлення вже використовується")
                        continue
                    metrics.increment("messages_processed")
                    recipients = await run_blocking(database.channel_member_usernames, channel_id)
                    await federation.publish(await make_message_event(message), durable=True)
                    await manager.broadcast({"type": "message", "message": message}, recipients=recipients)
                    await websocket.send_json(
                        {
                            "type": "message_ack",
                            "client_id": message_id,
                            "message_id": message_id,
                            "room_id": channel_id,
                            "duplicate": False,
                        }
                    )
                elif event_type == "message_receipt":
                    message_id = str(payload.get("message_id", ""))[:80]
                    device_id = str(payload.get("device_id", ""))[:80]
                    status = str(payload.get("status", ""))
                    message = await run_blocking(database.get_message, message_id)
                    device = await run_blocking(database.device_key, device_id)
                    if (
                        status not in {"delivered", "read"}
                        or not message
                        or not device
                        or device["username"] != username
                        or message["author_id"] == user["id"]
                        or not await run_blocking(
                            database.user_can_access_channel, message["room_id"], username
                        )
                    ):
                        continue
                    receipt_time = time.time()
                    receipt = {
                        "message_id": message_id,
                        "device_id": device_id,
                        "username": username,
                        "delivered_at": receipt_time,
                        "read_at": receipt_time if status == "read" else None,
                        "origin_node": settings.node_id,
                    }
                    await run_blocking(database.upsert_message_receipt, receipt)
                    # повторне додавання детермінованої події закриває проміжок між записом ack у sqlite та його записом у outbox
                    await federation.publish(await make_receipt_event(receipt), durable=True)
                    summary = await notify_message_author(message_id)
                    await websocket.send_json(
                        {
                            "type": "message_receipt_ack",
                            "message_id": message_id,
                            "status": status,
                            "summary": summary,
                        }
                    )
                elif event_type == "channel_key_request":
                    channel_id = str(payload.get("channel_id", ""))[:80]
                    device_id = str(payload.get("device_id", ""))[:80]
                    device = await run_blocking(database.device_key, device_id)
                    if (
                        not device
                        or device["username"] != username
                        or not await run_blocking(database.user_can_access_channel, channel_id, username)
                    ):
                        continue
                    recipients = await run_blocking(database.channel_member_usernames, channel_id)
                    if not recipients:
                        continue
                    data = {
                        "channel_id": channel_id,
                        "requester": username,
                        "device": public_device(device),
                    }
                    await manager.broadcast(
                        {"type": "channel_key_request", **data}, recipients=recipients
                    )
                    await federation.publish(await make_event("channel_key_request", data), durable=False)
                elif event_type == "presentation_state":
                    raw_recipients = payload.get("recipients", [])
                    recipients = (
                        list(dict.fromkeys(str(item) for item in raw_recipients if str(item) != username))[:10]
                        if isinstance(raw_recipients, list)
                        else []
                    )
                    document_id = str(payload.get("document_id", ""))[:80]
                    action = str(payload.get("action", "update"))
                    session_id = str(payload.get("session_id") or f"legacy:{username}")[:80]
                    if action not in {"start", "update", "stop"} or not recipients or not session_id:
                        continue
                    document = await run_blocking(database.get_document, document_id, False)
                    if action != "stop":
                        if not document or not await run_blocking(
                            database.user_can_access_document, document_id, username
                        ):
                            continue
                        allowed_recipients = await run_blocking(
                            database.channel_member_usernames, document["channel_id"]
                        )
                        if allowed_recipients is not None:
                            allowed_names = set(allowed_recipients)
                            recipients = [name for name in recipients if name in allowed_names]
                            if not recipients:
                                continue
                    try:
                        page = min(max(int(payload.get("page", 1)), 1), 1000)
                        sequence = min(max(int(payload.get("sequence", 0)), 0), 2_147_483_647)
                        raw_scroll = float(payload.get("scroll", 0))
                        raw_scroll_x = float(payload.get("scroll_x", 0))
                        raw_zoom = float(payload.get("zoom", 1))
                        raw_viewport_width = float(payload.get("viewport_width", 0))
                        raw_viewport_height = float(payload.get("viewport_height", 0))
                        if not all(math.isfinite(value) for value in (
                            raw_scroll, raw_scroll_x, raw_zoom, raw_viewport_width, raw_viewport_height
                        )):
                            continue
                        scroll = min(max(raw_scroll, 0.0), 1.0)
                        scroll_x = min(max(raw_scroll_x, 0.0), 1.0)
                        zoom = min(max(raw_zoom, 0.5), 2.0)
                        viewport_width = (
                            min(max(round(raw_viewport_width), 160), 7680) if raw_viewport_width > 0 else 0
                        )
                        viewport_height = (
                            min(max(round(raw_viewport_height), 120), 4320) if raw_viewport_height > 0 else 0
                        )
                    except (TypeError, ValueError):
                        continue
                    raw_cursor = payload.get("cursor")
                    cursor = None
                    if isinstance(raw_cursor, dict):
                        try:
                            raw_cursor_x = float(raw_cursor.get("x", 0))
                            raw_cursor_y = float(raw_cursor.get("y", 0))
                            if not math.isfinite(raw_cursor_x) or not math.isfinite(raw_cursor_y):
                                raise ValueError("non-finite cursor")
                            cursor = {
                                "x": min(max(raw_cursor_x, 0.0), 1.0),
                                "y": min(max(raw_cursor_y, 0.0), 1.0),
                            }
                            space = str(raw_cursor.get("space", "viewport"))
                            if "space" in raw_cursor and space in {"viewport", "text", "image"}:
                                cursor["space"] = space
                            if "viewport_x" in raw_cursor:
                                viewport_x = float(raw_cursor["viewport_x"])
                                if not math.isfinite(viewport_x):
                                    raise ValueError("non-finite viewport x")
                                cursor["viewport_x"] = min(max(viewport_x, 0.0), 1.0)
                            if "viewport_y" in raw_cursor:
                                viewport_y = float(raw_cursor["viewport_y"])
                                if not math.isfinite(viewport_y):
                                    raise ValueError("non-finite viewport y")
                                cursor["viewport_y"] = min(max(viewport_y, 0.0), 1.0)
                            if space == "text" and "text_offset" in raw_cursor:
                                cursor["text_offset"] = min(
                                    max(int(raw_cursor["text_offset"]), 0), 10_000_000
                                )
                        except (TypeError, ValueError):
                            cursor = None
                    data = {
                        "sender": username,
                        "sender_name": user["display_name"],
                        "action": action,
                        "session_id": session_id,
                        "sequence": sequence,
                        "document_id": document_id,
                        "channel_id": (document or {}).get("channel_id") or str(payload.get("channel_id", "lobby"))[:80],
                        "page": page,
                        "scroll": scroll,
                        "scroll_x": scroll_x,
                        "zoom": zoom,
                        "viewport_width": viewport_width,
                        "viewport_height": viewport_height,
                        "cursor": cursor,
                        "recipients": recipients,
                    }
                    metrics.increment("presentation_updates")
                    await manager.broadcast({"type": "presentation_state", **data}, recipients=recipients)
                    await federation.publish(await make_event("presentation_state", data), durable=False)
                elif event_type == "audio_chunk":
                    raw_recipients = payload.get("recipients", [])
                    recipients = [str(item) for item in raw_recipients][:10] if isinstance(raw_recipients, list) else []
                    samples = str(payload.get("samples", ""))
                    if not recipients or not samples or len(samples) > 180_000:
                        continue
                    try:
                        base64.b64decode(samples, validate=True)
                        sample_rate = min(max(int(payload.get("sample_rate", 48000)), 8000), 96000)
                        sequence = max(int(payload.get("sequence", 0)), 0)
                    except (ValueError, TypeError):
                        continue
                    data = {
                        "sender": username,
                        "sender_name": user["display_name"],
                        "recipients": recipients,
                        "samples": samples,
                        "sample_rate": sample_rate,
                        "sequence": sequence,
                    }
                    metrics.increment("audio_chunks")
                    await manager.broadcast({"type": "audio_chunk", **data}, recipients=recipients)
                    await federation.publish(await make_event("audio_chunk", data), durable=False)
                else:
                    await websocket.send_json({"type": "error", "message": "Невідома команда"})
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("WebSocket failure for %s", username)
        finally:
            metrics.increment("websocket_connections", -1)
            became_offline = await manager.disconnect(username, websocket)
            if became_offline:
                await federation.publish(
                    await make_event(
                        "presence",
                        {"username": username, "display_name": user["display_name"], "online": False},
                    ),
                    durable=False,
                )
            await broadcast_users()

    app.mount("/static", StaticFiles(directory=static_directory), name="static")

    @app.get("/docs", include_in_schema=False)
    async def api_documentation() -> FileResponse:
        return FileResponse(static_directory / "api-docs.html")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_directory / "index.html")

    return app
