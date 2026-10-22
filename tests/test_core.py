import tempfile
import time
import unittest
import uuid
import sqlite3
from contextlib import closing
from pathlib import Path

from app.database import Database
from app.security import (
    federation_signature,
    hash_password,
    token_digest,
    verify_federation_signature,
    verify_password,
)


class SecurityTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        salt_a, digest_a = hash_password("correct horse battery staple")
        salt_b, digest_b = hash_password("correct horse battery staple")
        self.assertNotEqual(salt_a, salt_b)
        self.assertNotEqual(digest_a, digest_b)
        self.assertTrue(verify_password("correct horse battery staple", salt_a, digest_a))
        self.assertFalse(verify_password("incorrect", salt_a, digest_a))

    def test_federation_signature_detects_tampering(self):
        timestamp = str(int(time.time()))
        body = b'{"type":"message"}'
        signature = federation_signature("shared-secret", timestamp, body)
        self.assertTrue(verify_federation_signature("shared-secret", timestamp, body, signature))
        self.assertFalse(verify_federation_signature("shared-secret", timestamp, body + b"x", signature))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "test.db")
        self.database.initialize()
        self.user = {
            "id": str(uuid.uuid4()),
            "username": "test_user",
            "display_name": "Test User",
            "password_salt": "aa",
            "password_hash": "bb",
            "created_at": time.time(),
            "origin_node": "test-node",
        }
        self.database.create_user(self.user)

    def tearDown(self):
        self.temp.cleanup()

    def test_session_and_message_roundtrip(self):
        token = "session-token"
        self.database.create_session(token_digest(token), self.user["id"], time.time() + 60)
        self.assertEqual(self.database.user_by_session(token_digest(token))["username"], "test_user")
        message = {
            "id": str(uuid.uuid4()), "room_id": "lobby", "author_id": self.user["id"],
            "author_name": "Test User", "body": "Hello", "created_at": time.time(),
            "lamport": 1, "origin_node": "test-node",
        }
        self.assertTrue(self.database.insert_message(message))
        self.assertFalse(self.database.insert_message(message))
        self.assertEqual(self.database.get_message(message["id"])["body"], "Hello")
        self.assertEqual(self.database.list_messages()[0]["body"], "Hello")
        self.assertEqual(self.database.user_by_id(self.user["id"])["username"], "test_user")

        delivered = {
            "message_id": message["id"], "device_id": "reader-device", "username": "reader",
            "delivered_at": time.time(),
            "read_at": None, "origin_node": "reader-node",
        }
        self.assertTrue(self.database.upsert_message_receipt(delivered))
        self.assertFalse(self.database.upsert_message_receipt(delivered))
        read = {**delivered, "read_at": delivered["delivered_at"] + 1}
        self.assertTrue(self.database.upsert_message_receipt(read))
        self.assertFalse(self.database.upsert_message_receipt(read))
        self.assertEqual(
            self.database.message_receipt_summary(message["id"]),
            {
                "delivered_count": 1, "read_count": 1,
                "delivered_users": ["reader"], "read_users": ["reader"],
            },
        )
        self.assertEqual(
            self.database.list_messages()[0]["receipt_summary"],
            {
                "delivered_count": 1, "read_count": 1,
                "delivered_users": ["reader"], "read_users": ["reader"],
            },
        )

    def test_messages_are_displayed_by_creation_time_not_late_delivery_order(self):
        now = time.time()
        messages = [
            {
                "id": "newer-message", "room_id": "lobby", "author_id": self.user["id"],
                "author_name": "Test User", "body": "today", "created_at": now,
                "lamport": 3, "origin_node": "node-a",
            },
            {
                "id": "delayed-old-message", "room_id": "lobby", "author_id": self.user["id"],
                "author_name": "Test User", "body": "delivered later", "created_at": now - 86400,
                "lamport": 99, "origin_node": "node-b",
            },
        ]
        for message in messages:
            self.assertTrue(self.database.insert_message(message))

        ordered = self.database.list_messages()
        self.assertEqual([item["id"] for item in ordered], ["delayed-old-message", "newer-message"])

    def test_legacy_receipts_are_migrated_to_per_device_schema(self):
        legacy = Database(Path(self.temp.name) / "legacy.db")
        with closing(legacy.connect()) as db, db:
            db.execute(
                """CREATE TABLE message_receipts (
                       message_id TEXT NOT NULL, username TEXT NOT NULL,
                       delivered_at REAL NOT NULL, read_at REAL, origin_node TEXT NOT NULL,
                       PRIMARY KEY(message_id, username)
                   )"""
            )
            db.execute(
                "INSERT INTO message_receipts VALUES (?, ?, ?, ?, ?)",
                ("legacy-message", "reader", 10.0, 11.0, "legacy-node"),
            )
        legacy.initialize()
        with closing(legacy.connect()) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(message_receipts)")}
        self.assertIn("device_id", columns)
        self.assertEqual(legacy.message_receipt_summary("legacy-message")["read_users"], ["reader"])

    def test_outbox_is_idempotent(self):
        event = {"id": "event-1", "type": "message", "data": {"body": "hello"}}
        self.database.enqueue("event-1", "http://peer", event)
        self.database.enqueue("event-1", "http://peer", event)
        self.assertEqual(len(self.database.due_outbox()), 1)
        self.database.outbox_delivered("event-1", "http://peer")
        self.assertEqual(self.database.due_outbox(), [])

    def test_outbox_retargets_obsolete_peer_and_retries_immediately(self):
        event = {"id": "event-network-change", "type": "message", "data": {"body": "offline"}}
        old_peer = "http://192.168.0.104:8001"
        new_peers = ["http://127.0.0.1:8001/", "http://127.0.0.1:8003"]
        self.database.enqueue(event["id"], old_peer, event)
        self.database.outbox_failed(event["id"], old_peer, 5)

        self.assertEqual(self.database.retarget_outbox(new_peers), 1)
        pending = self.database.due_outbox()
        self.assertEqual({row["peer"] for row in pending}, {"http://127.0.0.1:8001", "http://127.0.0.1:8003"})
        self.assertTrue(all(row["attempts"] == 0 and row["next_attempt_at"] == 0 for row in pending))
        self.assertEqual(self.database.retarget_outbox(new_peers), 0)

    def test_outbox_is_preserved_when_no_peer_is_configured(self):
        event = {"id": "event-no-network", "type": "message", "data": {"body": "offline"}}
        self.database.enqueue(event["id"], "http://temporarily-unavailable", event)
        self.assertEqual(self.database.retarget_outbox([]), 0)
        self.assertEqual(self.database.due_outbox()[0]["peer"], "http://temporarily-unavailable")

    def test_initial_snapshot_copies_persistent_data_but_not_sessions_or_outbox(self):
        channel_id = str(uuid.uuid4())
        device_id = str(uuid.uuid4())
        self.database.upsert_device_key({
            "device_id": device_id,
            "user_id": self.user["id"],
            "username": self.user["username"],
            "public_key": "{}",
            "created_at": time.time(),
            "origin_node": "test-node",
        })
        self.database.insert_channel_bundle(
            {
                "id": channel_id,
                "name": "snapshot-channel",
                "description": "initial replication",
                "owner_id": self.user["id"],
                "owner_name": self.user["display_name"],
                "is_private": 1,
                "encrypted": 1,
                "created_at": time.time(),
                "origin_node": "test-node",
            },
            [{
                "channel_id": channel_id,
                "user_id": self.user["id"],
                "username": self.user["username"],
                "role": "owner",
            }],
            [{
                "channel_id": channel_id,
                "recipient_device_id": device_id,
                "recipient_username": self.user["username"],
                "sender_device_id": device_id,
                "sender_public_key": "{}",
                "wrapped_key": "wrapped",
                "nonce": "nonce",
                "created_at": time.time(),
            }],
        )
        message_id = str(uuid.uuid4())
        self.database.insert_message({
            "id": message_id,
            "room_id": channel_id,
            "author_id": self.user["id"],
            "author_name": self.user["display_name"],
            "body": "snapshot message",
            "encrypted": 0,
            "ciphertext": None,
            "nonce": None,
            "created_at": time.time(),
            "lamport": 42,
            "origin_node": "test-node",
        })
        self.database.upsert_message_receipt({
            "message_id": message_id,
            "device_id": device_id,
            "username": self.user["username"],
            "delivered_at": time.time(),
            "read_at": time.time(),
            "origin_node": "test-node",
        })
        document_id = str(uuid.uuid4())
        self.database.insert_document({
            "id": document_id,
            "channel_id": channel_id,
            "owner_id": self.user["id"],
            "owner_name": self.user["display_name"],
            "name": "snapshot.txt",
            "media_type": "text/plain",
            "kind": "text",
            "page_count": 1,
            "content": b"snapshot document",
            "created_at": time.time(),
            "origin_node": "test-node",
        })
        self.database.mark_processed("event-already-applied")
        self.database.create_session(token_digest("local-only"), self.user["id"], time.time() + 60)
        self.database.enqueue("local-outbox", "http://peer", {"id": "local-outbox"})

        snapshot = self.database.export_snapshot()
        replica = Database(Path(self.temp.name) / "replica.db")
        replica.initialize()
        self.assertTrue(replica.needs_initial_replication())
        stats = replica.import_snapshot(snapshot, "test-node")
        replica.import_snapshot(snapshot, "test-node")

        self.assertEqual(stats["max_lamport"], 42)
        self.assertEqual(replica.user_by_username(self.user["username"])["id"], self.user["id"])
        self.assertEqual(replica.get_message(message_id)["body"], "snapshot message")
        self.assertEqual(replica.message_receipt_summary(message_id)["read_count"], 1)
        self.assertEqual(replica.get_document(document_id)["content"], b"snapshot document")
        self.assertEqual(replica.key_envelope(channel_id, device_id, self.user["username"])["wrapped_key"], "wrapped")
        self.assertFalse(replica.needs_initial_replication())
        with closing(replica.connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM processed_events WHERE id='event-already-applied'").fetchone()[0],
                1,
            )

    def test_documents_are_scoped_to_channel_members(self):
        channel_id = str(uuid.uuid4())
        self.database.insert_channel_bundle(
            {
                "id": channel_id,
                "name": "private-documents",
                "description": "",
                "owner_id": self.user["id"],
                "owner_name": self.user["display_name"],
                "is_private": 1,
                "encrypted": 1,
                "created_at": time.time(),
                "origin_node": "test-node",
            },
            [{"channel_id": channel_id, "user_id": self.user["id"], "username": self.user["username"], "role": "owner"}],
            [],
        )
        document_id = str(uuid.uuid4())
        self.database.insert_document(
            {
                "id": document_id,
                "channel_id": channel_id,
                "owner_id": self.user["id"],
                "owner_name": self.user["display_name"],
                "name": "private.txt",
                "media_type": "text/plain",
                "kind": "text",
                "page_count": 1,
                "content": b"private",
                "created_at": time.time(),
                "origin_node": "test-node",
            }
        )
        self.assertEqual(len(self.database.list_documents(channel_id, self.user["username"])), 1)
        self.assertEqual(self.database.list_documents(channel_id, "outsider"), [])
        self.assertTrue(self.database.user_can_access_document(document_id, self.user["username"]))
        self.assertFalse(self.database.user_can_access_document(document_id, "outsider"))

    def test_legacy_documents_migrate_to_lobby(self):
        legacy_path = Path(self.temp.name) / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            connection.execute(
                """CREATE TABLE documents (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, owner_name TEXT NOT NULL,
                    name TEXT NOT NULL, media_type TEXT NOT NULL, kind TEXT NOT NULL,
                    page_count INTEGER NOT NULL, content BLOB NOT NULL, created_at REAL NOT NULL,
                    origin_node TEXT NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("legacy-doc", "owner", "Owner", "old.txt", "text/plain", "text", 1, b"old", time.time(), "old-node"),
            )
        legacy_database = Database(legacy_path)
        legacy_database.initialize()
        self.assertEqual(legacy_database.get_document("legacy-doc", False)["channel_id"], "lobby")


if __name__ == "__main__":
    unittest.main()
