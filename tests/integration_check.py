"""End-to-end audit for three temporary Nexus nodes.

Run explicitly: python tests/integration_check.py
"""

import base64
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

import httpx
from websockets.sync.client import connect


ROOT = Path(__file__).resolve().parents[1]
PORT_A = 8121
PORT_B = 8122
PORT_C = 8123
BASE_A = f"http://127.0.0.1:{PORT_A}"
BASE_B = f"http://127.0.0.1:{PORT_B}"
BASE_C = f"http://127.0.0.1:{PORT_C}"
STALE_BASE_B = "http://127.0.0.1:8199"
PASSWORD = "Test-password-123"


def wait_ready(client: httpx.Client, base: str, timeout: float = 8) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get(f"{base}/api/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Node did not start: {base}")


def wait_until(check: Callable[[], object], timeout: float = 18, label: str = "condition"):
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            last_error = exc
        time.sleep(0.15)
    raise AssertionError(f"Timed out waiting for {label}; last error: {last_error}")


def receive_type(socket, event_type: str, timeout: float = 6) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = json.loads(socket.recv(timeout=min(1, deadline - time.time())))
        except TimeoutError:
            continue
        if event.get("type") == event_type:
            return event
    raise AssertionError(f"WebSocket event was not received: {event_type}")


def start_node(command: list[str]) -> subprocess.Popen:
    return subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        command_a = [
            sys.executable, "run_node.py", "--node-id", "test-a", "--host", "127.0.0.1",
            "--port", str(PORT_A), "--database", str(Path(temporary) / "a.db"), "--peer", BASE_B,
        ]
        command_a_with_stale_peer = [
            sys.executable, "run_node.py", "--node-id", "test-a", "--host", "127.0.0.1",
            "--port", str(PORT_A), "--database", str(Path(temporary) / "a.db"), "--peer", STALE_BASE_B,
        ]
        command_b = [
            sys.executable, "run_node.py", "--node-id", "test-b", "--host", "127.0.0.1",
            "--port", str(PORT_B), "--database", str(Path(temporary) / "b.db"), "--peer", BASE_A,
        ]
        command_c = [
            sys.executable, "run_node.py", "--node-id", "test-c", "--host", "127.0.0.1",
            "--port", str(PORT_C), "--database", str(Path(temporary) / "c.db"), "--peer", BASE_B,
        ]
        processes = [start_node(command_a_with_stale_peer)]
        try:
            with httpx.Client(timeout=5) as client:
                wait_ready(client, BASE_A)
                page = client.get(BASE_A)
                assert page.status_code == 200 and "Nexus" in page.text
                assert client.get(f"{BASE_A}/static/favicon.svg").status_code == 200
                assert "Content-Security-Policy" in page.headers
                assert page.headers.get("X-Content-Type-Options") == "nosniff"
                assert client.get(f"{BASE_A}/api/users").status_code == 401
                docs = client.get(f"{BASE_A}/docs")
                assert docs.status_code == 200 and "Локальна документація API" in docs.text
                assert "/static/api-docs.js" in docs.text and "cdn.jsdelivr.net" not in docs.text
                assert client.get(f"{BASE_A}/static/api-docs.js").status_code == 200
                assert client.get(f"{BASE_A}/static/api-docs.css").status_code == 200
                assert client.get(f"{BASE_A}/openapi.json").status_code == 200

                alice = client.post(
                    f"{BASE_A}/api/auth/register",
                    json={"username": "integration_alice", "display_name": "Alice", "password": PASSWORD},
                ).raise_for_status().json()
                headers_a = {"Authorization": f"Bearer {alice['token']}"}

                offline_message = client.post(
                    f"{BASE_A}/api/messages", headers=headers_a,
                    json={"body": "queued while peer offline", "room_id": "lobby"},
                ).raise_for_status().json()["message"]
                system_while_offline = client.get(f"{BASE_A}/api/system", headers=headers_a).raise_for_status().json()
                assert system_while_offline["database"]["outbox"] >= 2

                # зміна wi-fi або ip імітується повторним запуском вузла a з актуальною адресою peer
                # стару адресу stale_base_b у черзі потрібно автоматично замінити під час запуску
                processes[0].terminate()
                processes[0].wait(timeout=5)
                processes[0] = start_node(command_a)
                wait_ready(client, BASE_A)

                # вузол-одержувач запускається лише після запису стійких подій у перенаправлений outbox
                processes.append(start_node(command_b))
                wait_ready(client, BASE_B)

                def login_alice_on_peer():
                    response = client.post(
                        f"{BASE_B}/api/auth/login",
                        json={"username": "integration_alice", "password": PASSWORD},
                    )
                    return response.json() if response.status_code == 200 else None

                alice_peer = wait_until(login_alice_on_peer, label="offline user replication")
                headers_alice_b = {"Authorization": f"Bearer {alice_peer['token']}"}

                def offline_message_arrived():
                    items = client.get(f"{BASE_B}/api/messages", headers=headers_alice_b).raise_for_status().json()["messages"]
                    return any(item["id"] == offline_message["id"] for item in items)

                wait_until(offline_message_arrived, label="outbox recovery")
                wait_until(
                    lambda: client.get(f"{BASE_A}/api/system", headers=headers_a).raise_for_status().json()["database"]["outbox"] == 0,
                    label="outbox drain",
                )

                bob = client.post(
                    f"{BASE_B}/api/auth/register",
                    json={"username": "integration_bob", "display_name": "Bob", "password": PASSWORD},
                ).raise_for_status().json()
                headers_b = {"Authorization": f"Bearer {bob['token']}"}
                wait_until(
                    lambda: any(
                        user["username"] == "integration_bob"
                        for user in client.get(f"{BASE_A}/api/users", headers=headers_a).raise_for_status().json()["users"]
                    ),
                    label="user replication",
                )

                alice_device_id = str(uuid.uuid4())
                bob_device_id = str(uuid.uuid4())
                alice_public = {
                    "kty": "EC", "crv": "P-256",
                    "x": base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("="),
                    "y": base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("="),
                }
                bob_public = {
                    "kty": "EC", "crv": "P-256",
                    "x": base64.urlsafe_b64encode(b"c" * 32).decode().rstrip("="),
                    "y": base64.urlsafe_b64encode(b"d" * 32).decode().rstrip("="),
                }
                client.post(
                    f"{BASE_A}/api/keys/devices", headers=headers_a,
                    json={"device_id": alice_device_id, "public_key": alice_public},
                ).raise_for_status()
                client.post(
                    f"{BASE_B}/api/keys/devices", headers=headers_b,
                    json={"device_id": bob_device_id, "public_key": bob_public},
                ).raise_for_status()
                wait_until(
                    lambda: len(
                        client.get(
                            f"{BASE_A}/api/keys/devices?usernames=integration_alice,integration_bob",
                            headers=headers_a,
                        ).raise_for_status().json()["devices"]
                    ) == 2,
                    label="device-key replication",
                )

                encrypted_channel_id = str(uuid.uuid4())
                test_wrapped_key = base64.b64encode(b"wrapped-channel-key-for-test").decode("ascii")
                test_nonce = base64.b64encode(b"123456789012").decode("ascii")
                channel = client.post(
                    f"{BASE_A}/api/channels", headers=headers_a,
                    json={
                        "id": encrypted_channel_id,
                        "name": "encrypted-test",
                        "description": "E2EE federation test",
                        "member_usernames": ["integration_bob"],
                        "encrypted": True,
                        "key_envelopes": [
                            {
                                "recipient_device_id": alice_device_id,
                                "recipient_username": "integration_alice",
                                "sender_device_id": alice_device_id,
                                "sender_public_key": alice_public,
                                "wrapped_key": test_wrapped_key,
                                "nonce": test_nonce,
                            },
                            {
                                "recipient_device_id": bob_device_id,
                                "recipient_username": "integration_bob",
                                "sender_device_id": alice_device_id,
                                "sender_public_key": alice_public,
                                "wrapped_key": test_wrapped_key,
                                "nonce": test_nonce,
                            },
                        ],
                    },
                ).raise_for_status().json()["channel"]
                assert channel["encrypted"] == 1
                wait_until(
                    lambda: any(
                        item["id"] == encrypted_channel_id
                        for item in client.get(f"{BASE_B}/api/channels", headers=headers_b).raise_for_status().json()["channels"]
                    ),
                    label="encrypted-channel replication",
                )
                key_envelope = client.get(
                    f"{BASE_B}/api/channels/{encrypted_channel_id}/key?device_id={bob_device_id}",
                    headers=headers_b,
                ).raise_for_status().json()["key_envelope"]
                assert key_envelope["wrapped_key"] == test_wrapped_key

                secret_plaintext = "this text must never reach SQLite"
                encrypted_payload = base64.b64encode(b"synthetic-aes-gcm-ciphertext").decode("ascii")
                encrypted_nonce = base64.b64encode(b"abcdefghijkl").decode("ascii")
                encrypted_message = client.post(
                    f"{BASE_A}/api/messages", headers=headers_a,
                    json={
                        "room_id": encrypted_channel_id,
                        "body": secret_plaintext,
                        "encrypted": True,
                        "ciphertext": encrypted_payload,
                        "nonce": encrypted_nonce,
                    },
                ).raise_for_status().json()["message"]
                assert encrypted_message["body"] == "" and encrypted_message["ciphertext"] == encrypted_payload
                with sqlite3.connect(Path(temporary) / "a.db") as audit_database:
                    stored_body, stored_ciphertext = audit_database.execute(
                        "SELECT body, ciphertext FROM messages WHERE id=?", (encrypted_message["id"],)
                    ).fetchone()
                assert stored_body == "" and stored_ciphertext == encrypted_payload
                assert secret_plaintext not in stored_body

                def encrypted_message_arrived():
                    items = client.get(
                        f"{BASE_B}/api/messages?room_id={encrypted_channel_id}", headers=headers_b
                    ).raise_for_status().json()["messages"]
                    return next((item for item in items if item["id"] == encrypted_message["id"]), None)

                replicated_encrypted_message = wait_until(
                    encrypted_message_arrived, label="encrypted-message replication"
                )
                assert replicated_encrypted_message["body"] == ""
                assert secret_plaintext not in json.dumps(replicated_encrypted_message)

                mallory = client.post(
                    f"{BASE_B}/api/auth/register",
                    json={"username": "integration_mallory", "display_name": "Mallory", "password": PASSWORD},
                ).raise_for_status().json()
                headers_mallory = {"Authorization": f"Bearer {mallory['token']}"}
                assert client.get(
                    f"{BASE_B}/api/messages?room_id={encrypted_channel_id}", headers=headers_mallory
                ).status_code == 403

                invalid_file = client.post(
                    f"{BASE_A}/api/documents", headers=headers_a,
                    files={"file": ("program.exe", b"MZ", "application/octet-stream")},
                    data={"kind": "text", "page_count": "1"},
                )
                assert invalid_file.status_code == 415

                document = client.post(
                    f"{BASE_A}/api/documents", headers=headers_a,
                    files={"file": ("demo.txt", b"Nexus document", "text/plain")},
                    data={"kind": "text", "page_count": "1", "channel_id": encrypted_channel_id},
                ).raise_for_status().json()["document"]
                assert document["channel_id"] == encrypted_channel_id

                def replicated_document():
                    items = client.get(
                        f"{BASE_B}/api/documents?channel_id={encrypted_channel_id}", headers=headers_b
                    ).raise_for_status().json()["documents"]
                    return next((item for item in items if item["id"] == document["id"]), None)

                wait_until(replicated_document, label="document replication")
                content = client.get(
                    f"{BASE_B}/api/documents/{document['id']}/content", headers=headers_b
                ).raise_for_status().content
                assert content == b"Nexus document"
                assert client.get(f"{BASE_B}/api/documents/{document['id']}/content").status_code == 401
                assert client.get(
                    f"{BASE_B}/api/documents?channel_id={encrypted_channel_id}", headers=headers_mallory
                ).status_code == 403
                assert client.get(
                    f"{BASE_B}/api/documents/{document['id']}/content", headers=headers_mallory
                ).status_code == 403

                with connect(
                    f"ws://127.0.0.1:{PORT_A}/ws", subprotocols=["nexus", f"auth.{alice['token']}"]
                ) as socket_a, connect(
                    f"ws://127.0.0.1:{PORT_B}/ws", subprotocols=["nexus", f"auth.{bob['token']}"]
                ) as socket_b:
                    assert receive_type(socket_a, "hello")["node_id"] == "test-a"
                    assert receive_type(socket_b, "hello")["node_id"] == "test-b"

                    socket_a.send(json.dumps({"type": "message_send", "body": None}))
                    assert receive_type(socket_a, "error")["message"]

                    browser_message_id = str(uuid.uuid4())
                    browser_payload = {
                        "type": "message_send", "body": "websocket hello",
                        "client_id": browser_message_id, "client_created_at": time.time(),
                    }
                    socket_a.send(json.dumps(browser_payload))
                    acknowledgement = receive_type(socket_a, "message_ack")
                    assert acknowledgement["client_id"] == browser_message_id
                    assert acknowledgement["duplicate"] is False
                    received_message = receive_type(socket_b, "message")
                    assert received_message["message"]["body"] == "websocket hello"
                    assert received_message["message"]["id"] == browser_message_id

                    socket_b.send(json.dumps({
                        "type": "message_receipt", "message_id": browser_message_id,
                        "device_id": bob_device_id, "status": "delivered",
                    }))
                    delivered_ack = receive_type(socket_b, "message_receipt_ack")
                    assert delivered_ack["status"] == "delivered"
                    delivered_update = receive_type(socket_a, "message_receipt")
                    assert delivered_update["message_id"] == browser_message_id
                    assert delivered_update["summary"] == {
                        "delivered_count": 1, "read_count": 0,
                        "delivered_users": ["integration_bob"], "read_users": [],
                    }

                    socket_b.send(json.dumps({
                        "type": "message_receipt", "message_id": browser_message_id,
                        "device_id": bob_device_id, "status": "read",
                    }))
                    read_ack = receive_type(socket_b, "message_receipt_ack")
                    assert read_ack["status"] == "read"
                    read_update = receive_type(socket_a, "message_receipt")
                    assert read_update["summary"] == {
                        "delivered_count": 1, "read_count": 1,
                        "delivered_users": ["integration_bob"], "read_users": ["integration_bob"],
                    }

                    def replicated_receipt():
                        messages = client.get(
                            f"{BASE_A}/api/messages?room_id=lobby", headers=headers_a
                        ).raise_for_status().json()["messages"]
                        current = next(item for item in messages if item["id"] == browser_message_id)
                        return current["receipt_summary"]["read_count"] == 1

                    wait_until(replicated_receipt, label="message read receipt replication")

                    socket_a.send(json.dumps(browser_payload))
                    duplicate_acknowledgement = receive_type(socket_a, "message_ack")
                    assert duplicate_acknowledgement["client_id"] == browser_message_id
                    assert duplicate_acknowledgement["duplicate"] is True
                    stored_once = client.get(
                        f"{BASE_A}/api/messages?room_id=lobby", headers=headers_a
                    ).raise_for_status().json()["messages"]
                    assert sum(item["id"] == browser_message_id for item in stored_once) == 1

                    presentation_session = str(uuid.uuid4())
                    socket_a.send(json.dumps({
                        "type": "presentation_state", "action": "start",
                        "session_id": presentation_session, "sequence": 1,
                        "document_id": document["id"], "page": 1, "scroll": 2,
                        "scroll_x": -1, "zoom": 9,
                        "viewport_width": 99999, "viewport_height": 10,
                        "cursor": {"x": 2, "y": -1}, "recipients": ["integration_bob"],
                    }))
                    presentation = receive_type(socket_b, "presentation_state")
                    assert presentation["document_id"] == document["id"]
                    assert presentation["cursor"] == {"x": 1.0, "y": 0.0}
                    assert presentation["action"] == "start"
                    assert presentation["session_id"] == presentation_session
                    assert presentation["sequence"] == 1 and presentation["scroll"] == 1.0
                    assert presentation["scroll_x"] == 0.0 and presentation["zoom"] == 2.0
                    assert presentation["viewport_width"] == 7680
                    assert presentation["viewport_height"] == 120

                    socket_a.send(json.dumps({
                        "type": "presentation_state", "action": "stop",
                        "session_id": presentation_session, "sequence": 2,
                        "document_id": document["id"], "page": 1, "scroll": 0,
                        "cursor": None, "recipients": ["integration_bob"],
                    }))
                    presentation_stop = receive_type(socket_b, "presentation_state")
                    assert presentation_stop["action"] == "stop"
                    assert presentation_stop["session_id"] == presentation_session

                    encoded_samples = base64.b64encode(b"\x00\x00\x01\x00").decode("ascii")
                    socket_a.send(json.dumps({
                        "type": "audio_chunk", "samples": encoded_samples, "sample_rate": 48000,
                        "sequence": 0, "recipients": ["integration_bob"],
                    }))
                    audio = receive_type(socket_b, "audio_chunk")
                    assert audio["samples"] == encoded_samples and audio["sender"] == "integration_alice"

                # умова завдання передбачає конференції до десяти одночасних користувачів
                ten_tokens = [alice_peer["token"], bob["token"], mallory["token"]]
                for index in range(7):
                    extra = client.post(
                        f"{BASE_B}/api/auth/register",
                        json={
                            "username": f"participant_{index}",
                            "display_name": f"Participant {index}",
                            "password": PASSWORD,
                        },
                    ).raise_for_status().json()
                    ten_tokens.append(extra["token"])
                meeting_sockets = []
                try:
                    for token in ten_tokens:
                        socket = connect(
                            f"ws://127.0.0.1:{PORT_B}/ws",
                            subprotocols=["nexus", f"auth.{token}"],
                            legacy=True,
                        )
                        meeting_sockets.append(socket)
                        receive_type(socket, "hello")
                    system_with_meeting = client.get(
                        f"{BASE_B}/api/system", headers=headers_b
                    ).raise_for_status().json()
                    assert system_with_meeting["metrics"]["websocket_connections"] == 10
                finally:
                    for socket in meeting_sockets:
                        socket.close()

                # новий порожній вузол має автоматично отримати повний підписаний знімок peer-вузла
                assert client.post(f"{BASE_B}/api/federation/snapshot", json={}).status_code == 401
                processes.append(start_node(command_c))
                wait_ready(client, BASE_C)

                def login_bob_on_new_node():
                    response = client.post(
                        f"{BASE_C}/api/auth/login",
                        json={"username": "integration_bob", "password": PASSWORD},
                    )
                    return response.json() if response.status_code == 200 else None

                bob_on_c = wait_until(login_bob_on_new_node, timeout=25, label="initial user replication")
                headers_c = {"Authorization": f"Bearer {bob_on_c['token']}"}
                channels_c = client.get(
                    f"{BASE_C}/api/channels", headers=headers_c
                ).raise_for_status().json()["channels"]
                assert any(item["id"] == encrypted_channel_id for item in channels_c)
                messages_c = client.get(
                    f"{BASE_C}/api/messages?room_id={encrypted_channel_id}", headers=headers_c
                ).raise_for_status().json()["messages"]
                assert any(item["id"] == encrypted_message["id"] for item in messages_c)
                lobby_c = client.get(
                    f"{BASE_C}/api/messages?room_id=lobby", headers=headers_c
                ).raise_for_status().json()["messages"]
                bootstrapped_browser_message = next(item for item in lobby_c if item["id"] == browser_message_id)
                assert bootstrapped_browser_message["receipt_summary"]["read_count"] == 1
                envelope_c = client.get(
                    f"{BASE_C}/api/channels/{encrypted_channel_id}/key?device_id={bob_device_id}",
                    headers=headers_c,
                ).raise_for_status().json()["key_envelope"]
                assert envelope_c["wrapped_key"] == test_wrapped_key
                documents_c = client.get(
                    f"{BASE_C}/api/documents?channel_id={encrypted_channel_id}", headers=headers_c
                ).raise_for_status().json()["documents"]
                assert any(item["id"] == document["id"] for item in documents_c)
                assert client.get(
                    f"{BASE_C}/api/documents/{document['id']}/content", headers=headers_c
                ).raise_for_status().content == b"Nexus document"

                client.delete(f"{BASE_A}/api/documents/{document['id']}", headers=headers_a).raise_for_status()
                wait_until(
                    lambda: all(
                        item["id"] != document["id"]
                        for item in client.get(
                            f"{BASE_B}/api/documents?channel_id={encrypted_channel_id}", headers=headers_b
                        ).raise_for_status().json()["documents"]
                    ),
                    label="document deletion replication",
                )
                client.post(f"{BASE_A}/api/auth/logout", headers=headers_a).raise_for_status()
                assert client.get(f"{BASE_A}/api/me", headers=headers_a).status_code == 401

                print(
                    "OK: security headers/auth, offline outbox recovery, REST/WebSocket federation, "
                    "delivery/read receipts, private E2EE channels, documents, targeted presentation/audio, "
                    "10 concurrent users and initial replication to a new third node"
                )
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    main()
