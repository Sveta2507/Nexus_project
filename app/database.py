import base64
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        # кожна фонова операція отримує власне з’єднання, а busy_timeout дає їй дочекатися звільнення sqlite
        connection = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            # режим wal дозволяє читати дані, поки інший потік завершує запис
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    origin_node TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    body TEXT NOT NULL,
                    encrypted INTEGER NOT NULL DEFAULT 0,
                    ciphertext TEXT,
                    nonce TEXT,
                    created_at REAL NOT NULL,
                    lamport INTEGER NOT NULL,
                    origin_node TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_room_order
                    ON messages(room_id, lamport, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_room_created
                    ON messages(room_id, created_at, lamport, id);
                CREATE TABLE IF NOT EXISTS message_receipts (
                    message_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    delivered_at REAL NOT NULL,
                    read_at REAL,
                    origin_node TEXT NOT NULL,
                    PRIMARY KEY(message_id, device_id)
                );
                CREATE INDEX IF NOT EXISTS idx_message_receipts_message
                    ON message_receipts(message_id, read_at, delivered_at);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL DEFAULT 'lobby',
                    owner_id TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    origin_node TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channels (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    owner_id TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    is_private INTEGER NOT NULL DEFAULT 1,
                    encrypted INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    origin_node TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_members (
                    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    PRIMARY KEY(channel_id, username)
                );
                CREATE INDEX IF NOT EXISTS idx_channel_members_username
                    ON channel_members(username, channel_id);
                CREATE TABLE IF NOT EXISTS device_keys (
                    device_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    public_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    origin_node TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_device_keys_username
                    ON device_keys(username);
                CREATE TABLE IF NOT EXISTS channel_key_envelopes (
                    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                    recipient_device_id TEXT NOT NULL,
                    recipient_username TEXT NOT NULL,
                    sender_device_id TEXT NOT NULL,
                    sender_public_key TEXT NOT NULL,
                    wrapped_key TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(channel_id, recipient_device_id)
                );
                CREATE TABLE IF NOT EXISTS processed_events (
                    id TEXT PRIMARY KEY,
                    processed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT NOT NULL,
                    peer TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(event_id, peer)
                );
                CREATE TABLE IF NOT EXISTS node_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # невеликі міграції оновлюють стару базу без видалення даних користувача
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)").fetchall()}
            if "encrypted" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0")
            if "ciphertext" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN ciphertext TEXT")
            if "nonce" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN nonce TEXT")
            receipt_columns = {
                row[1] for row in db.execute("PRAGMA table_info(message_receipts)").fetchall()
            }
            if "device_id" not in receipt_columns:
                db.execute("DROP INDEX IF EXISTS idx_message_receipts_message")
                db.execute("ALTER TABLE message_receipts RENAME TO message_receipts_legacy")
                db.execute(
                    """CREATE TABLE message_receipts (
                           message_id TEXT NOT NULL,
                           device_id TEXT NOT NULL,
                           username TEXT NOT NULL,
                           delivered_at REAL NOT NULL,
                           read_at REAL,
                           origin_node TEXT NOT NULL,
                           PRIMARY KEY(message_id, device_id)
                       )"""
                )
                db.execute(
                    """INSERT INTO message_receipts
                       (message_id, device_id, username, delivered_at, read_at, origin_node)
                       SELECT message_id, 'legacy:' || username, username, delivered_at, read_at, origin_node
                       FROM message_receipts_legacy"""
                )
                db.execute("DROP TABLE message_receipts_legacy")
                db.execute(
                    """CREATE INDEX idx_message_receipts_message
                       ON message_receipts(message_id, read_at, delivered_at)"""
                )
            document_columns = {row[1] for row in db.execute("PRAGMA table_info(documents)").fetchall()}
            if "channel_id" not in document_columns:
                db.execute("ALTER TABLE documents ADD COLUMN channel_id TEXT NOT NULL DEFAULT 'lobby'")
            db.execute("CREATE INDEX IF NOT EXISTS idx_documents_channel_created ON documents(channel_id, created_at DESC)")
            db.execute(
                """INSERT OR IGNORE INTO channels
                   (id, name, description, owner_id, owner_name, is_private, encrypted, created_at, origin_node)
                   VALUES ('lobby', 'загальний', 'Загальне командне обговорення', 'system', 'Nexus', 0, 0, 0, 'system')"""
            )

    def needs_initial_replication(self) -> bool:
        """визначає, чи це новий вузол без користувацьких даних і завершеного bootstrap."""
        with closing(self.connect()) as db:
            completed = db.execute(
                "SELECT 1 FROM node_meta WHERE key='initial_replication_complete'"
            ).fetchone()
            if completed:
                return False
            checks = (
                "SELECT 1 FROM users LIMIT 1",
                "SELECT 1 FROM messages LIMIT 1",
                "SELECT 1 FROM documents LIMIT 1",
                "SELECT 1 FROM device_keys LIMIT 1",
                "SELECT 1 FROM channels WHERE id<>'lobby' LIMIT 1",
            )
            return not any(db.execute(query).fetchone() for query in checks)

    def max_lamport(self) -> int:
        with closing(self.connect()) as db:
            return int(db.execute("SELECT COALESCE(MAX(lamport), 0) FROM messages").fetchone()[0])

    def export_snapshot(self) -> dict[str, Any]:
        """створює повний знімок постійних даних без сесій і peer-specific outbox."""
        with closing(self.connect()) as db, db:
            # одна read-транзакція фіксує узгоджений стан усіх таблиць на момент експорту
            db.execute("BEGIN")
            def rows(table: str) -> list[dict[str, Any]]:
                return [dict(row) for row in db.execute(f"SELECT * FROM {table}").fetchall()]

            documents = rows("documents")
            for document in documents:
                document["content_base64"] = base64.b64encode(document.pop("content")).decode("ascii")
            snapshot = {
                "version": 1,
                "generated_at": time.time(),
                "max_lamport": int(
                    db.execute("SELECT COALESCE(MAX(lamport), 0) FROM messages").fetchone()[0]
                ),
                "users": rows("users"),
                "channels": rows("channels"),
                "channel_members": rows("channel_members"),
                "device_keys": rows("device_keys"),
                "channel_key_envelopes": rows("channel_key_envelopes"),
                "messages": rows("messages"),
                "message_receipts": rows("message_receipts"),
                "documents": documents,
                "processed_events": rows("processed_events"),
            }
            snapshot["record_count"] = (
                len(snapshot["users"])
                + len(snapshot["messages"])
                + len(snapshot["documents"])
                + len(snapshot["device_keys"])
                + sum(channel["id"] != "lobby" for channel in snapshot["channels"])
            )
            return snapshot

    def import_snapshot(self, snapshot: dict[str, Any], source: str) -> dict[str, int]:
        """атомарно додає знімок peer-вузла, не перезаписуючи локальні повідомлення й документи."""
        if int(snapshot.get("version", 0)) != 1:
            raise ValueError("Непідтримувана версія знімка")
        try:
            snapshot_max_lamport = max(int(snapshot.get("max_lamport", 0)), 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Некоректний логічний час знімка") from exc

        limits = {
            "users": 10_000,
            "channels": 10_000,
            "channel_members": 100_000,
            "device_keys": 100_000,
            "channel_key_envelopes": 500_000,
            "messages": 500_000,
            "message_receipts": 1_000_000,
            "documents": 2_000,
            "processed_events": 1_000_000,
        }

        def records(name: str) -> list[dict[str, Any]]:
            value = snapshot.get(name, [])
            if (
                not isinstance(value, list)
                or len(value) > limits[name]
                or any(not isinstance(item, dict) for item in value)
            ):
                raise ValueError(f"Некоректний розділ знімка: {name}")
            return value

        users = records("users")
        channels = records("channels")
        members = records("channel_members")
        devices = records("device_keys")
        envelopes = records("channel_key_envelopes")
        messages = records("messages")
        receipts = records("message_receipts")
        processed = records("processed_events")
        documents = []
        for item in records("documents"):
            document = item.copy()
            try:
                content = base64.b64decode(document.pop("content_base64"), validate=True)
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError("Некоректний вміст документа у знімку") from exc
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("Документ у знімку перевищує 8 МБ")
            document["content"] = content
            documents.append(document)

        with closing(self.connect()) as db, db:
            db.executemany(
                """INSERT OR IGNORE INTO users
                   (id, username, display_name, password_salt, password_hash, created_at, origin_node)
                   VALUES (:id, :username, :display_name, :password_salt, :password_hash, :created_at, :origin_node)""",
                users,
            )
            db.executemany(
                """INSERT OR IGNORE INTO channels
                   (id, name, description, owner_id, owner_name, is_private, encrypted, created_at, origin_node)
                   VALUES (:id, :name, :description, :owner_id, :owner_name, :is_private, :encrypted,
                           :created_at, :origin_node)""",
                channels,
            )
            db.executemany(
                """INSERT OR IGNORE INTO channel_members(channel_id, user_id, username, role)
                   VALUES (:channel_id, :user_id, :username, :role)""",
                members,
            )
            db.executemany(
                """INSERT INTO device_keys
                   (device_id, user_id, username, public_key, created_at, origin_node)
                   VALUES (:device_id, :user_id, :username, :public_key, :created_at, :origin_node)
                   ON CONFLICT(device_id) DO UPDATE SET
                     user_id=excluded.user_id, username=excluded.username,
                     public_key=excluded.public_key, created_at=excluded.created_at""",
                devices,
            )
            db.executemany(
                """INSERT OR REPLACE INTO channel_key_envelopes
                   (channel_id, recipient_device_id, recipient_username, sender_device_id,
                    sender_public_key, wrapped_key, nonce, created_at)
                   VALUES (:channel_id, :recipient_device_id, :recipient_username, :sender_device_id,
                           :sender_public_key, :wrapped_key, :nonce, :created_at)""",
                envelopes,
            )
            db.executemany(
                """INSERT OR IGNORE INTO messages
                   (id, room_id, author_id, author_name, body, encrypted, ciphertext, nonce,
                    created_at, lamport, origin_node)
                   VALUES (:id, :room_id, :author_id, :author_name, :body, :encrypted, :ciphertext, :nonce,
                           :created_at, :lamport, :origin_node)""",
                messages,
            )
            db.executemany(
                """INSERT INTO message_receipts
                   (message_id, device_id, username, delivered_at, read_at, origin_node)
                   VALUES (:message_id, :device_id, :username, :delivered_at, :read_at, :origin_node)
                   ON CONFLICT(message_id, device_id) DO UPDATE SET
                     delivered_at=MIN(message_receipts.delivered_at, excluded.delivered_at),
                     read_at=CASE
                       WHEN message_receipts.read_at IS NULL THEN excluded.read_at
                       WHEN excluded.read_at IS NULL THEN message_receipts.read_at
                       ELSE MIN(message_receipts.read_at, excluded.read_at)
                     END,
                     origin_node=excluded.origin_node""",
                receipts,
            )
            db.executemany(
                """INSERT OR IGNORE INTO documents
                   (id, channel_id, owner_id, owner_name, name, media_type, kind, page_count,
                    content, created_at, origin_node)
                   VALUES (:id, :channel_id, :owner_id, :owner_name, :name, :media_type, :kind, :page_count,
                           :content, :created_at, :origin_node)""",
                documents,
            )
            db.executemany(
                "INSERT OR IGNORE INTO processed_events(id, processed_at) VALUES (:id, :processed_at)",
                processed,
            )
            db.execute(
                """INSERT INTO node_meta(key, value) VALUES ('initial_replication_complete', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (json.dumps({"source": source, "completed_at": time.time()}),),
            )

        imported_max = max((int(message.get("lamport", 0)) for message in messages), default=0)
        imported_records = (
            len(users)
            + len(messages)
            + len(documents)
            + len(devices)
            + sum(channel.get("id") != "lobby" for channel in channels)
        )
        return {
            "records": imported_records,
            "max_lamport": max(snapshot_max_lamport, imported_max),
        }

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def create_user(self, user: dict[str, Any]) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO users
                   (id, username, display_name, password_salt, password_hash, created_at, origin_node)
                   VALUES (:id, :username, :display_name, :password_salt, :password_hash, :created_at, :origin_node)""",
                user,
            )

    def upsert_user(self, user: dict[str, Any]) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO users
                   (id, username, display_name, password_salt, password_hash, created_at, origin_node)
                   VALUES (:id, :username, :display_name, :password_salt, :password_hash, :created_at, :origin_node)
                   ON CONFLICT(username) DO UPDATE SET
                     display_name=excluded.display_name,
                     password_salt=excluded.password_salt,
                     password_hash=excluded.password_hash""",
                user,
            )

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db, db:
            return self._row(db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())

    def create_session(self, token_hash: str, user_id: str, expires_at: float) -> None:
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            db.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (token_hash, user_id, expires_at),
            )

    def user_by_session(self, token_hash: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db, db:
            row = db.execute(
                """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>?""",
                (token_hash, time.time()),
            ).fetchone()
            return self._row(row)

    def delete_session(self, token_hash: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def list_users(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db, db:
            rows = db.execute(
                "SELECT id, username, display_name, created_at, origin_node FROM users ORDER BY display_name COLLATE NOCASE"
            ).fetchall()
            return [dict(row) for row in rows]

    def insert_message(self, message: dict[str, Any]) -> bool:
        # унікальний id і insert or ignore роблять повторне одержання тієї самої події безпечним
        values = {"encrypted": 0, "ciphertext": None, "nonce": None, "body": "", **message}
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO messages
                   (id, room_id, author_id, author_name, body, encrypted, ciphertext, nonce,
                    created_at, lamport, origin_node)
                   VALUES (:id, :room_id, :author_id, :author_name, :body, :encrypted, :ciphertext, :nonce,
                           :created_at, :lamport, :origin_node)""",
                values,
            )
            return cursor.rowcount > 0

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            return self._row(row)

    def list_messages(self, room_id: str = "lobby", limit: int = 100) -> list[dict[str, Any]]:
        # спочатку вибираються найновіші записи, після чого вони повертаються клієнту у хронологічному порядку
        with closing(self.connect()) as db, db:
            rows = db.execute(
                """SELECT * FROM (
                       SELECT * FROM messages WHERE room_id=?
                       ORDER BY created_at DESC, lamport DESC, id DESC LIMIT ?
                   ) ORDER BY created_at ASC, lamport ASC, id ASC""",
                (room_id, min(max(limit, 1), 500)),
            ).fetchall()
            messages = [dict(row) for row in rows]
            if not messages:
                return messages
            message_ids = [message["id"] for message in messages]
            placeholders = ",".join("?" for _ in message_ids)
            receipt_rows = db.execute(
                f"""SELECT message_id, username, read_at FROM message_receipts
                    WHERE message_id IN ({placeholders})""",
                message_ids,
            ).fetchall()
            summaries: dict[str, dict[str, Any]] = {}
            for row in receipt_rows:
                summary = summaries.setdefault(
                    row["message_id"],
                    {"delivered_count": 0, "read_count": 0, "delivered_users": set(), "read_users": set()},
                )
                summary["delivered_count"] += 1
                summary["delivered_users"].add(str(row["username"]))
                if row["read_at"] is not None:
                    summary["read_count"] += 1
                    summary["read_users"].add(str(row["username"]))
            for summary in summaries.values():
                summary["delivered_users"] = sorted(summary["delivered_users"])
                summary["read_users"] = sorted(summary["read_users"])
            for message in messages:
                message["receipt_summary"] = summaries.get(
                    message["id"],
                    {"delivered_count": 0, "read_count": 0, "delivered_users": [], "read_users": []},
                )
            return messages

    def user_by_id(self, user_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            return self._row(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def upsert_message_receipt(self, receipt: dict[str, Any]) -> bool:
        # повторний ack не повинен зсувати вже зафіксований час доставки або прочитання вперед
        delivered_at = float(receipt["delivered_at"])
        read_at = float(receipt["read_at"]) if receipt.get("read_at") is not None else None
        with closing(self.connect()) as db, db:
            current = db.execute(
                "SELECT delivered_at, read_at FROM message_receipts WHERE message_id=? AND device_id=?",
                (receipt["message_id"], receipt["device_id"]),
            ).fetchone()
            if current:
                next_delivered = min(float(current["delivered_at"]), delivered_at)
                current_read = float(current["read_at"]) if current["read_at"] is not None else None
                next_read = (
                    current_read
                    if read_at is None
                    else read_at if current_read is None else min(current_read, read_at)
                )
                changed = next_delivered != float(current["delivered_at"]) or next_read != current_read
                if changed:
                    db.execute(
                        """UPDATE message_receipts SET delivered_at=?, read_at=?, origin_node=?
                           WHERE message_id=? AND device_id=?""",
                        (
                            next_delivered,
                            next_read,
                            receipt["origin_node"],
                            receipt["message_id"],
                            receipt["device_id"],
                        ),
                    )
                return changed
            db.execute(
                """INSERT INTO message_receipts
                   (message_id, device_id, username, delivered_at, read_at, origin_node)
                   VALUES (:message_id, :device_id, :username, :delivered_at, :read_at, :origin_node)""",
                receipt,
            )
            return True

    def message_receipt_summary(self, message_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT username, read_at FROM message_receipts WHERE message_id=?",
                (message_id,),
            ).fetchall()
            return {
                "delivered_count": len(rows),
                "read_count": sum(row["read_at"] is not None for row in rows),
                "delivered_users": sorted({str(row["username"]) for row in rows}),
                "read_users": sorted({str(row["username"]) for row in rows if row["read_at"] is not None}),
            }

    def insert_document(self, document: dict[str, Any]) -> bool:
        values = {"channel_id": "lobby", **document}
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO documents
                   (id, channel_id, owner_id, owner_name, name, media_type, kind, page_count, content, created_at, origin_node)
                   VALUES (:id, :channel_id, :owner_id, :owner_name, :name, :media_type, :kind, :page_count,
                           :content, :created_at, :origin_node)""",
                values,
            )
            return cursor.rowcount > 0

    def list_documents(self, channel_id: str, username: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db, db:
            rows = db.execute(
                """SELECT d.id, d.channel_id, d.owner_id, d.owner_name, d.name, d.media_type, d.kind,
                          d.page_count, d.created_at, d.origin_node, length(d.content) AS size
                   FROM documents d
                   JOIN channels c ON c.id=d.channel_id
                   LEFT JOIN channel_members m ON m.channel_id=c.id AND m.username=?
                   WHERE d.channel_id=? AND (c.is_private=0 OR m.username IS NOT NULL)
                   ORDER BY d.created_at DESC""",
                (username, channel_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_document(self, document_id: str, include_content: bool = True) -> dict[str, Any] | None:
        columns = "*" if include_content else (
            "id, channel_id, owner_id, owner_name, name, media_type, kind, page_count, created_at, origin_node, "
            "length(content) AS size"
        )
        with closing(self.connect()) as db, db:
            return self._row(db.execute(f"SELECT {columns} FROM documents WHERE id=?", (document_id,)).fetchone())

    def user_can_access_document(self, document_id: str, username: str) -> bool:
        with closing(self.connect()) as db, db:
            row = db.execute(
                """SELECT 1 FROM documents d
                   JOIN channels c ON c.id=d.channel_id
                   LEFT JOIN channel_members m ON m.channel_id=c.id AND m.username=?
                   WHERE d.id=? AND (c.is_private=0 OR m.username IS NOT NULL)""",
                (username, document_id),
            ).fetchone()
            return row is not None

    def delete_document(self, document_id: str, owner_id: str) -> bool:
        with closing(self.connect()) as db, db:
            return db.execute(
                "DELETE FROM documents WHERE id=? AND owner_id=?", (document_id, owner_id)
            ).rowcount > 0

    def upsert_device_key(self, device: dict[str, Any]) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO device_keys
                   (device_id, user_id, username, public_key, created_at, origin_node)
                   VALUES (:device_id, :user_id, :username, :public_key, :created_at, :origin_node)
                   ON CONFLICT(device_id) DO UPDATE SET
                     user_id=excluded.user_id, username=excluded.username,
                     public_key=excluded.public_key, created_at=excluded.created_at""",
                device,
            )

    def list_device_keys(self, usernames: list[str] | None = None) -> list[dict[str, Any]]:
        with closing(self.connect()) as db, db:
            if usernames:
                placeholders = ",".join("?" for _ in usernames)
                rows = db.execute(
                    f"SELECT * FROM device_keys WHERE username IN ({placeholders}) ORDER BY created_at",
                    usernames,
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM device_keys ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]

    def device_key(self, device_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db, db:
            return self._row(db.execute("SELECT * FROM device_keys WHERE device_id=?", (device_id,)).fetchone())

    def insert_channel_bundle(
        self,
        channel: dict[str, Any],
        members: list[dict[str, Any]],
        envelopes: list[dict[str, Any]],
    ) -> bool:
        # канал, учасники і загорнуті ключі записуються однією транзакцією або не записуються взагалі
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO channels
                   (id, name, description, owner_id, owner_name, is_private, encrypted, created_at, origin_node)
                   VALUES (:id, :name, :description, :owner_id, :owner_name, :is_private, :encrypted,
                           :created_at, :origin_node)""",
                channel,
            )
            for member in members:
                db.execute(
                    """INSERT OR IGNORE INTO channel_members(channel_id, user_id, username, role)
                       VALUES (:channel_id, :user_id, :username, :role)""",
                    member,
                )
            for envelope in envelopes:
                db.execute(
                    """INSERT OR REPLACE INTO channel_key_envelopes
                       (channel_id, recipient_device_id, recipient_username, sender_device_id,
                        sender_public_key, wrapped_key, nonce, created_at)
                       VALUES (:channel_id, :recipient_device_id, :recipient_username, :sender_device_id,
                               :sender_public_key, :wrapped_key, :nonce, :created_at)""",
                    envelope,
                )
            return cursor.rowcount > 0

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db, db:
            return self._row(db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone())

    def list_channels_for_user(self, username: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db, db:
            rows = db.execute(
                """SELECT DISTINCT c.* FROM channels c
                   LEFT JOIN channel_members m ON m.channel_id=c.id
                   WHERE c.is_private=0 OR m.username=?
                   ORDER BY c.created_at, c.name COLLATE NOCASE""",
                (username,),
            ).fetchall()
            result = []
            for row in rows:
                channel = dict(row)
                members = db.execute(
                    "SELECT username, role FROM channel_members WHERE channel_id=? ORDER BY username",
                    (channel["id"],),
                ).fetchall()
                channel["members"] = [dict(member) for member in members]
                result.append(channel)
            return result

    def channel_member_usernames(self, channel_id: str) -> list[str] | None:
        with closing(self.connect()) as db, db:
            channel = db.execute("SELECT is_private FROM channels WHERE id=?", (channel_id,)).fetchone()
            if not channel:
                return []
            if not bool(channel["is_private"]):
                return None
            rows = db.execute(
                "SELECT username FROM channel_members WHERE channel_id=?", (channel_id,)
            ).fetchall()
            return [str(row["username"]) for row in rows]

    def user_can_access_channel(self, channel_id: str, username: str) -> bool:
        with closing(self.connect()) as db, db:
            row = db.execute(
                """SELECT 1 FROM channels c LEFT JOIN channel_members m ON m.channel_id=c.id AND m.username=?
                   WHERE c.id=? AND (c.is_private=0 OR m.username IS NOT NULL)""",
                (username, channel_id),
            ).fetchone()
            return row is not None

    def insert_key_envelope(self, envelope: dict[str, Any]) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT OR REPLACE INTO channel_key_envelopes
                   (channel_id, recipient_device_id, recipient_username, sender_device_id,
                    sender_public_key, wrapped_key, nonce, created_at)
                   VALUES (:channel_id, :recipient_device_id, :recipient_username, :sender_device_id,
                           :sender_public_key, :wrapped_key, :nonce, :created_at)""",
                envelope,
            )

    def key_envelope(self, channel_id: str, device_id: str, username: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db, db:
            return self._row(
                db.execute(
                    """SELECT * FROM channel_key_envelopes
                       WHERE channel_id=? AND recipient_device_id=? AND recipient_username=?""",
                    (channel_id, device_id, username),
                ).fetchone()
            )

    def mark_processed(self, event_id: str) -> bool:
        # ця таблиця забезпечує ідемпотентність: одна розподілена подія застосовується лише один раз
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO processed_events(id, processed_at) VALUES (?, ?)",
                (event_id, time.time()),
            )
            return cursor.rowcount > 0

    def enqueue(self, event_id: str, peer: str, payload: dict[str, Any]) -> None:
        # кожен вузол-одержувач має окремий стан доставки тієї самої події
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO outbox(event_id, peer, payload) VALUES (?, ?, ?)",
                (event_id, peer, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )

    def retarget_outbox(self, peers: list[str]) -> int:
        """переносить недоставлені події зі старих адрес вузлів на актуальні."""
        active_peers = list(dict.fromkeys(peer.rstrip("/") for peer in peers if peer.strip()))
        if not active_peers:
            return 0
        placeholders = ",".join("?" for _ in active_peers)
        with closing(self.connect()) as db, db:
            stale = db.execute(
                f"SELECT event_id, payload FROM outbox WHERE peer NOT IN ({placeholders})",
                active_peers,
            ).fetchall()
            if not stale:
                return 0
            for row in stale:
                for peer in active_peers:
                    db.execute(
                        """INSERT INTO outbox(event_id, peer, payload, attempts, next_attempt_at)
                           VALUES (?, ?, ?, 0, 0)
                           ON CONFLICT(event_id, peer) DO UPDATE SET
                             payload=excluded.payload,
                             attempts=0,
                             next_attempt_at=0""",
                        (row["event_id"], peer, row["payload"]),
                    )
            db.execute(
                f"DELETE FROM outbox WHERE peer NOT IN ({placeholders})",
                active_peers,
            )
            return len(stale)

    def due_outbox(self, limit: int = 25) -> list[dict[str, Any]]:
        with closing(self.connect()) as db, db:
            rows = db.execute(
                "SELECT * FROM outbox WHERE next_attempt_at<=? ORDER BY attempts, rowid LIMIT ?",
                (time.time(), limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def outbox_delivered(self, event_id: str, peer: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM outbox WHERE event_id=? AND peer=?", (event_id, peer))

    def outbox_failed(self, event_id: str, peer: str, attempts: int) -> None:
        # експоненційна затримка не дозволяє недоступному вузлу створити безперервний потік повторів
        delay = min(30, 2 ** min(attempts, 5))
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE outbox SET attempts=?, next_attempt_at=? WHERE event_id=? AND peer=?",
                (attempts, time.time() + delay, event_id, peer),
            )

    def counts(self) -> dict[str, int]:
        with closing(self.connect()) as db, db:
            result: dict[str, int] = {}
            for table in (
                "users", "messages", "message_receipts", "documents", "channels", "device_keys", "outbox"
            ):
                result[table] = int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            return result
