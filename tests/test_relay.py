import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kitty_remote.buffer import BufferAssembler, frames
from kitty_remote.protocol import CreateWindow, Input, Target, packet
from kitty_remote.relay import create_app
from kitty_remote.settings import Settings

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(tmp_path / "relay.sqlite", ORIGIN, dev=True))
    app.state.store.create_admin("admin", "secure-test-password")
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def login(c):
    assert (
        c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "secure-test-password"},
            headers={"origin": ORIGIN},
        ).status_code
        == 200
    )


def pair(c):
    p = c.post("/api/pairings/start", json={"name": "Test desktop"}).json()
    info = c.post(
        "/api/pairings/lookup", json={"code": p["code"]}, headers={"origin": ORIGIN}
    ).json()
    assert (
        c.post(
            "/api/pairings/approve",
            json={"code": p["code"], "pairing_id": info["id"]},
            headers={"origin": ORIGIN},
        ).status_code
        == 200
    )
    return c.post(
        "/api/pairings/claim", json={"pairing_id": p["pairing_id"], "secret": p["secret"]}
    ).json()


def until(ws, kind):
    for _ in range(30):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"did not receive {kind}")


def test_origin_auth_and_http_limits(client):
    assert client.get("/api/devices").status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"username": "admin", "password": "secure-test-password"}
        ).status_code
        == 403
    )
    assert client.post("/api/pairings/start", content="x" * 9000).status_code == 413
    login(client)
    assert client.get("/api/devices").status_code == 200
    assert "HttpOnly" in client.cookies.get("kr_session", "") or client.cookies.get("kr_session")
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "ws://127.0.0.1:8765/ws/browser", headers={"origin": "https://evil.test"}
        ),
    ):
        pass


def test_continuous_buffer_routing_and_resync(client):
    login(client)
    credentials = pair(client)
    target = Target(
        device_id=credentials["device_id"],
        agent_epoch=str(uuid.uuid4()),
        kitty_instance_id=str(uuid.uuid4()),
        window_id=1,
    )
    window = dict(
        target=target.model_dump(),
        title="history",
        cwd="/tmp",
        os_window_id=1,
        tab_id=1,
        rows=24,
        cols=80,
        alternate=False,
    )
    full = packet(
        "buffer",
        target=target.model_dump(),
        seq=1,
        sampled_at=time.time(),
        rows=24,
        cols=80,
        alternate=False,
        content=("中文🙂" + "x" * 100 + "\n") * 10000,
    )

    def receive_buffer(ws, assembler):
        first = until(ws, "buffer_start")
        assembler.push(first)
        for _ in range(1024):
            part = ws.receive_json()
            if part["type"] in {"buffer_chunk", "buffer_end"}:
                result = assembler.push(part)
                if result:
                    return result
        raise AssertionError("buffer incomplete")

    with client.websocket_connect(
        "ws://127.0.0.1:8765/ws/agent", headers={"authorization": "Bearer " + credentials["token"]}
    ) as agent:
        agent.send_json(packet("hello", agent_epoch=target.agent_epoch, windows=[window]))
        with client.websocket_connect(
            "ws://127.0.0.1:8765/ws/browser", headers={"origin": ORIGIN}
        ) as browser:
            until(browser, "state")
            browser.send_json(packet("subscribe", target=target.model_dump(), buffer=True))
            assert until(agent, "subscribe")["buffer"] is True
            receiver = BufferAssembler()
            for part in frames(full, None):
                agent.send_json(part)
            assert receive_buffer(browser, receiver)["content"] == full["content"]
            changed = {**full, "seq": 2, "content": full["content"] + "new output"}
            for part in frames(changed, full):
                agent.send_json(part)
            assert receive_buffer(browser, receiver)["content"] == changed["content"]
            browser.send_json(packet("buffer_resync"))
            assert receive_buffer(browser, BufferAssembler())["content"] == changed["content"]
            assert until(agent, "buffer_resync")["target"] == target.model_dump()


def test_end_to_end_routing_lease_and_revoke(client):
    login(client)
    credentials = pair(client)
    target = Target(
        device_id=credentials["device_id"],
        agent_epoch=str(uuid.uuid4()),
        kitty_instance_id=str(uuid.uuid4()),
        window_id=1,
    )
    window = {
        "target": target.model_dump(),
        "title": "Test",
        "cwd": "/tmp",
        "os_window_id": 1,
        "tab_id": 1,
        "rows": 24,
        "cols": 80,
        "alternate": False,
    }
    with client.websocket_connect(
        "ws://127.0.0.1:8765/ws/agent", headers={"authorization": "Bearer " + credentials["token"]}
    ) as agent:
        agent.send_json(packet("hello", agent_epoch=target.agent_epoch, windows=[window]))
        with client.websocket_connect(
            "ws://127.0.0.1:8765/ws/browser", headers={"origin": ORIGIN}
        ) as browser:
            assert until(browser, "state")["devices"][0]["online"]
            browser.send_json(packet("subscribe", target=target.model_dump()))
            assert until(agent, "subscribe")["target"] == target.model_dump()
            assert until(browser, "lease")["owned"]
            agent.send_json(
                packet(
                    "snapshot",
                    target=target.model_dump(),
                    seq=1,
                    sampled_at=time.time(),
                    rows=24,
                    cols=80,
                    alternate=False,
                    content="中文🙂",
                )
            )
            assert until(browser, "snapshot")["content"] == "中文🙂"
            req = Input(
                request_id=str(uuid.uuid4()),
                target=target,
                op="text",
                text="任务",
                expires_at=time.time() + 9,
            )
            browser.send_json(req.model_dump())
            assert until(agent, "input")["text"] == "任务"
            assert until(browser, "receipt")["status"] == "received"
            agent.send_json(
                packet(
                    "receipt",
                    request_id=req.request_id,
                    target=target.model_dump(),
                    status="submitted",
                )
            )
            assert until(browser, "receipt")["status"] == "submitted"
            with client.websocket_connect(
                "ws://127.0.0.1:8765/ws/browser", headers={"origin": ORIGIN}
            ) as second:
                until(second, "state")
                second.send_json(packet("subscribe", target=target.model_dump()))
                assert not until(second, "lease")["owned"]
                second.send_json(
                    req.model_copy(update={"request_id": str(uuid.uuid4())}).model_dump()
                )
                assert until(second, "receipt")["status"] == "rejected"
            req2 = req.model_copy(update={"request_id": str(uuid.uuid4())})
            browser.send_json(req2.model_dump())
            until(agent, "input")
            until(browser, "receipt")
            assert (
                client.post(
                    f"/api/devices/{target.device_id}/revoke", json={}, headers={"origin": ORIGIN}
                ).status_code
                == 200
            )
            assert until(browser, "receipt")["status"] == "uncertain"
            assert client.get("/api/devices").json()["devices"] == []


def test_create_window_requires_lease_and_routes_confirmed_window(client):
    login(client)
    credentials = pair(client)
    source = Target(
        device_id=credentials["device_id"],
        agent_epoch=str(uuid.uuid4()),
        kitty_instance_id=str(uuid.uuid4()),
        window_id=1,
    )
    first = {
        "target": source.model_dump(),
        "title": "source",
        "cwd": "/tmp",
        "os_window_id": 1,
        "tab_id": 1,
        "rows": 24,
        "cols": 80,
        "alternate": False,
    }
    created = {
        **first,
        "target": source.model_copy(update={"window_id": 2}).model_dump(),
        "title": "shell",
        "cwd": "/home/test",
        "os_window_id": 2,
        "tab_id": 2,
    }
    req = CreateWindow(request_id=str(uuid.uuid4()), target=source, expires_at=time.time() + 9)
    with client.websocket_connect(
        "ws://127.0.0.1:8765/ws/agent", headers={"authorization": "Bearer " + credentials["token"]}
    ) as agent:
        agent.send_json(packet("hello", agent_epoch=source.agent_epoch, windows=[first]))
        with client.websocket_connect(
            "ws://127.0.0.1:8765/ws/browser", headers={"origin": ORIGIN}
        ) as browser:
            until(browser, "state")
            browser.send_json(req.model_dump())
            assert until(browser, "create_window_result")["status"] == "rejected"
            browser.send_json(packet("subscribe", target=source.model_dump()))
            until(agent, "subscribe")
            assert until(browser, "lease")["owned"]
            browser.send_json(req.model_dump())
            assert until(agent, "create_window")["request_id"] == req.request_id
            assert until(browser, "create_window_result")["status"] == "received"
            another = req.model_copy(update={"request_id": str(uuid.uuid4())})
            browser.send_json(another.model_dump())
            assert until(browser, "create_window_result")["status"] == "rejected"
            agent.send_json(
                packet("windows", agent_epoch=source.agent_epoch, windows=[first, created])
            )
            assert len(until(browser, "state")["devices"][0]["windows"]) == 2
            agent.send_json(
                packet(
                    "create_window_result",
                    request_id=req.request_id,
                    target=source.model_dump(),
                    status="created",
                    detail="",
                    window=created,
                )
            )
            result = until(browser, "create_window_result")
            assert result["status"] == "created"
            assert result["window"] == created


def test_expired_pairing_and_logout(client):
    login(client)
    pending = client.post("/api/pairings/start", json={"name": "x"}).json()
    with client.app.state.store.connect() as db:
        db.execute("UPDATE pairings SET expires=0")
    assert (
        client.post(
            "/api/pairings/lookup", json={"code": pending["code"]}, headers={"origin": ORIGIN}
        ).status_code
        == 404
    )
    with client.websocket_connect(
        "ws://127.0.0.1:8765/ws/browser", headers={"origin": ORIGIN}
    ) as ws:
        until(ws, "state")
        assert (
            client.post("/api/auth/logout", json={}, headers={"origin": ORIGIN}).status_code == 200
        )
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert client.get("/api/devices").status_code == 401
