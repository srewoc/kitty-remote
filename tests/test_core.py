import asyncio
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from kitty_remote.agent import Executor, relay_clock_offset
from kitty_remote.kitty import InputError, Kitty, KittyError, Record, WindowCreateError, command
from kitty_remote.protocol import CreateWindow, Input, Outbox, Target, Window, packet, sanitize_ansi
from kitty_remote.settings import Settings
from kitty_remote.storage import Store


def target():
    return Target(
        device_id=str(uuid.uuid4()),
        agent_epoch=str(uuid.uuid4()),
        kitty_instance_id=str(uuid.uuid4()),
        window_id=1,
    )


def request(t=None, **kw):
    return Input(
        request_id=str(uuid.uuid4()),
        target=t or target(),
        op="text",
        text="中文🙂",
        expires_at=time.time() + 10,
        **kw,
    )


def test_display_filter():
    text = (
        "中文🙂\x1b[31m红\x1b[0m\x1b]52;c;c2VjcmV0\x07!\x1b]8;;https://evil\x1b\\链接\x1b]8;;\x1b\\"
    )
    assert sanitize_ansi(text) == "中文🙂\x1b[31m红\x1b[0m!链接"
    assert sanitize_ansi("A\x90secret\x9cB\x1b[6n\x1b[2J\x9d52;c;x\x07C") == "ABC"
    assert sanitize_ansi("ok\x1b]truncated") == "ok"
    assert sanitize_ansi("\x1b[12;4H\x1b[?25h\x1b[2 q") == "\x1b[12;4H\x1b[?25h\x1b[2 q"


def test_input_boundaries():
    good = request()
    for update in (
        {"text": "中" * 22000},
        {"op": "keys", "text": "", "keys": ["shell"]},
        {"text": ""},
        {"expires_at": float("inf")},
        {"target": {**good.target.model_dump(), "window_id": 0}},
    ):
        with pytest.raises(ValidationError):
            Input.model_validate({**good.model_dump(), **update})


def test_create_window_has_no_command_or_path_fields():
    req = CreateWindow(request_id=str(uuid.uuid4()), target=target(), expires_at=time.time() + 10)
    for extra in ({"command": "codex"}, {"cwd": "/tmp"}, {"type": "input"}):
        with pytest.raises(ValidationError):
            CreateWindow.model_validate({**req.model_dump(), **extra})


async def test_backpressure_preserves_input_and_latest_screen():
    out = Outbox(2)
    t = target().model_dump()
    out.put(packet("snapshot", target=t, seq=1))
    out.put(packet("receipt", status="received"))
    out.put(packet("snapshot", target=t, seq=2))
    out.put(packet("receipt", status="submitted"))
    with pytest.raises(asyncio.QueueFull):
        out.put(packet("receipt"))
    assert (await out.get())["status"] == "received"
    assert (await out.get())["status"] == "submitted"
    assert (await out.get())["seq"] == 2
    assert not out.latest


async def test_bounded_process_output_is_not_truncated():
    value = await command(
        [sys.executable, "-c", "import sys; sys.stdout.write('x'*200000)"], limit=300000
    )
    assert len(value) == 200000
    with pytest.raises(KittyError):
        await command([sys.executable, "-c", "print('x'*200000)"], limit=100)


async def test_duplicate_and_conflicting_input():
    adapter = SimpleNamespace(execute=AsyncMock())
    executor = Executor(adapter)
    req = request()
    messages = []
    executor.enqueue(req, messages.append)
    executor.enqueue(req, messages.append)
    executor.enqueue(req.model_copy(update={"text": "changed"}), messages.append)
    await asyncio.sleep(0.02)
    executor.enqueue(req, messages.append)
    assert adapter.execute.await_count == 1
    assert any(m["status"] == "rejected" for m in messages)
    assert messages[-1]["status"] == "submitted"
    await executor.disconnect()


async def test_expired_and_disconnect_do_not_replay():
    gate = asyncio.Event()
    adapter = SimpleNamespace(execute=AsyncMock(side_effect=lambda req: None))

    async def waiting(req):
        await gate.wait()

    adapter.execute.side_effect = waiting
    executor = Executor(adapter)
    messages = []
    expired = request().model_copy(update={"expires_at": time.time() - 1})
    executor.enqueue(expired, messages.append)
    assert messages[-1]["status"] == "rejected"
    req = request()
    executor.enqueue(req, messages.append)
    await asyncio.sleep(0.01)
    await executor.disconnect()
    executor.enqueue(req, messages.append)
    assert messages[-1]["status"] == "uncertain"
    assert adapter.execute.await_count == 1


async def test_input_sequence_and_partial_failure():
    t = target()
    kitty = Kitty(t.device_id, t.agent_epoch)
    rec = Record(
        Window(
            target=t,
            title="test",
            cwd="/tmp",
            os_window_id=1,
            tab_id=1,
            rows=24,
            cols=80,
            alternate=False,
        ),
        "random",
        (1, 2, 3),
    )
    inst = SimpleNamespace(address="unix:/test")
    kitty.validate = AsyncMock(return_value=(inst, rec))
    kitty.call = AsyncMock(return_value=b"")
    req = request(t).model_copy(update={"op": "text_enter"})
    await kitty.execute(req)
    assert [c.args[1] for c in kitty.call.await_args_list] == ["send-text", "send-key"]
    assert kitty.call.await_args_list[0].kwargs["data"] == "中文🙂".encode()
    assert "id:1 and var:kitty_remote_token=random" in kitty.call.await_args_list[0].args
    kitty.call.reset_mock()
    kitty.call.side_effect = [b"", TimeoutError()]
    with pytest.raises(InputError) as caught:
        await kitty.execute(req)
    assert caught.value.status == "uncertain"
    kitty.call.reset_mock()
    kitty.validate.side_effect = KittyError("old marker")
    with pytest.raises(InputError) as caught:
        await kitty.execute(req)
    assert caught.value.status == "rejected"
    kitty.call.assert_not_called()


async def test_create_window_uses_verified_instance_and_home_directory():
    source = target()
    kitty = Kitty(source.device_id, source.agent_epoch)
    record = Record(
        Window(
            target=source,
            title="source",
            cwd="/tmp",
            os_window_id=1,
            tab_id=1,
            rows=24,
            cols=80,
            alternate=False,
        ),
        "marker",
        (1, 2, 3),
    )
    instance = SimpleNamespace(address="unix:/verified", id=source.kitty_instance_id)
    created = record.window.model_copy(
        update={
            "target": source.model_copy(update={"window_id": 2}),
            "cwd": str(Path.home()),
            "os_window_id": 2,
        }
    )
    kitty.validate = AsyncMock(return_value=(instance, record))
    kitty.call = AsyncMock(return_value=b"2\n")
    kitty.refresh = AsyncMock(return_value=[record.window, created])
    req = CreateWindow(request_id=str(uuid.uuid4()), target=source, expires_at=time.time() + 10)
    window, windows = await kitty.create_window(req)
    assert window == created and windows[-1] == created
    assert kitty.call.await_args.args == (
        "unix:/verified",
        "launch",
        "--type=os-window",
        "--source-window",
        "id:1 and var:kitty_remote_token=marker",
        "--cwd",
        str(Path.home()),
    )
    kitty.call.reset_mock()
    kitty.call.side_effect = TimeoutError()
    with pytest.raises(WindowCreateError) as caught:
        await kitty.create_window(req)
    assert caught.value.status == "uncertain"
    kitty.call.reset_mock()
    kitty.validate.side_effect = KittyError("old target")
    with pytest.raises(WindowCreateError) as caught:
        await kitty.create_window(req)
    assert caught.value.status == "rejected"
    kitty.call.assert_not_called()


async def test_duplicate_create_request_runs_only_once():
    source = target()
    window = Window(
        target=source.model_copy(update={"window_id": 2}),
        title="shell",
        cwd=str(Path.home()),
        os_window_id=2,
        tab_id=2,
        rows=24,
        cols=80,
        alternate=False,
    )
    adapter = SimpleNamespace(
        epoch=source.agent_epoch, create_window=AsyncMock(return_value=(window, [window]))
    )
    executor = Executor(adapter)
    req = CreateWindow(request_id=str(uuid.uuid4()), target=source, expires_at=time.time() + 10)
    messages = []
    executor.enqueue(req, messages.append)
    executor.enqueue(req, messages.append)
    await asyncio.sleep(0.02)
    executor.enqueue(req, messages.append)
    assert adapter.create_window.await_count == 1
    assert [msg["type"] for msg in messages if msg["type"] == "windows"] == ["windows"]
    assert messages[-1]["status"] == "created"
    assert messages[-1]["window"]["target"]["window_id"] == 2
    await executor.disconnect()


def test_storage_pairing_and_secrets(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.create_admin("admin", "long-test-password")
    token = store.login("admin", "long-test-password", 60)
    assert store.session(token)
    assert store.login("admin", "incorrect", 60) is None
    pending = store.start_pairing("My desktop")
    with pytest.raises(ValueError):
        store.claim_pairing(pending["pairing_id"], "bad-secret")
    assert store.claim_pairing(pending["pairing_id"], pending["secret"]) is None
    lookup = store.lookup_pairing(pending["code"])
    device_id = store.approve_pairing(pending["code"], lookup["id"])
    credential = store.claim_pairing(pending["pairing_id"], pending["secret"])
    assert credential["device_id"] == device_id
    assert store.authenticate_device(credential["token"]) == device_id
    raw = (tmp_path / "db.sqlite").read_bytes()
    for secret in (token, credential["token"], pending["secret"], "long-test-password"):
        assert secret.encode() not in raw
    store.revoke(device_id)
    assert store.authenticate_device(credential["token"]) is None
    store.logout(token)
    assert not store.session(token)
    assert (tmp_path / "db.sqlite").stat().st_mode & 0o777 == 0o600


def test_settings_https():
    with pytest.raises(ValueError):
        Settings(database="unused", origin="http://example.com")
    with pytest.raises(ValueError):
        Settings(database="unused", origin="http://example.com", dev=True)
    Settings(database="unused", origin="http://127.0.0.1:8765", dev=True)


async def test_old_epoch_refused():
    t = target()
    kitty = Kitty(t.device_id, str(uuid.uuid4()))
    with pytest.raises(KittyError):
        await kitty.validate(t)


async def test_window_close_reuse_and_marker_loss_rotate_identity(monkeypatch):
    import copy

    import kitty_remote.kitty as module

    identity = (1, 2, 3, 4, 5)
    monkeypatch.setattr(module, "socket_identity", lambda address: identity)
    kitty = Kitty(str(uuid.uuid4()), str(uuid.uuid4()), ["unix:/owned"])
    window = {
        "id": 1,
        "pid": 42,
        "created_at": 1.1,
        "user_vars": {},
        "lines": 24,
        "columns": 80,
        "in_alternate_screen": False,
        "title": "safe",
        "cwd": "/tmp",
    }
    rows = [({"id": 1}, {"id": 1}, window)]

    async def listing(address):
        return copy.deepcopy(rows)

    async def call(address, *args, **kw):
        assert address == "unix:/owned"
        if args[0] == "set-user-vars":
            window["user_vars"][module.MARKER] = args[-1].split("=", 1)[1]
        return b""

    kitty.raw_windows = listing
    kitty.call = call
    first = (await kitty.refresh())[0].target
    await kitty.validate(first)
    window["user_vars"].clear()
    with pytest.raises(KittyError):
        await kitty.validate(first)
    second = (await kitty.refresh())[0].target
    assert first != second
    rows.clear()
    assert await kitty.refresh() == []
    window["created_at"] = 2.2
    rows.append(({"id": 1}, {"id": 1}, window))
    third = (await kitty.refresh())[0].target
    assert third != second
    with pytest.raises(KittyError):
        await kitty.validate(second)
    identity = (9, 8, 7, 6, 5)
    fourth = (await kitty.refresh())[0].target
    assert fourth != third
    assert kitty.candidates() == ["unix:/owned"]


async def test_queue_bound_and_expiry_rechecked_before_submission():
    gate = asyncio.Event()

    async def blocked(req):
        await gate.wait()

    executor = Executor(SimpleNamespace(execute=blocked))
    messages = []
    t = target()
    for _ in range(17):
        executor.enqueue(request(t), messages.append)
    assert messages[-1]["status"] == "rejected"
    assert "队列" in messages[-1]["detail"]
    await executor.disconnect()


async def test_history_limits(monkeypatch):
    t = target()
    kitty = Kitty(t.device_id, t.agent_epoch)
    record = Record(
        Window(
            target=t,
            title="test",
            cwd="/tmp",
            os_window_id=1,
            tab_id=1,
            rows=24,
            cols=80,
            alternate=False,
        ),
        "token",
        (1, 2, 3),
    )
    kitty.validate = AsyncMock(return_value=(SimpleNamespace(address="unix:/test"), record))
    kitty.call = AsyncMock(return_value=("line\n" * 2001).encode())
    _, content = await kitty.screen(t, history=True)
    assert content == "line\n" * 2001
    assert kitty.call.call_args.kwargs["limit"] == 16 * 1024 * 1024


def test_encoded_message_limit():
    out = Outbox()
    with pytest.raises(ValueError, match="1 MiB"):
        out.put(packet("error", detail="\x01" * 180000))


@pytest.mark.parametrize("skew", [-3600, -34, 34, 3600])
def test_relay_clock_deadline_conversion(skew):
    local = time.time()
    relay = local + skew
    offset = relay_clock_offset(relay, 0.2, local)
    req = request().model_copy(update={"expires_at": relay + 10 - offset})
    assert req.expires_at == pytest.approx(local + 9.8)
    assert relay + 0.1 - offset < local  # expired conservatively in transit
    with pytest.raises(ValueError):
        relay_clock_offset(float("nan"), 0.2, local)
    with pytest.raises(ValueError):
        relay_clock_offset(relay, 10, local)


def test_socket_discovery_uses_only_the_project_pattern_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    for name in ["kitty-remote-1", "other-kitty-2", "kitty-remote-3"]:
        (tmp_path / name).touch()
    t = target()
    assert Kitty(t.device_id, t.agent_epoch).candidates() == [
        f"unix:{tmp_path}/kitty-remote-1",
        f"unix:{tmp_path}/kitty-remote-3",
    ]
    assert Kitty(t.device_id, t.agent_epoch, socket_glob=f"{tmp_path}/other-*").candidates() == [
        f"unix:{tmp_path}/other-kitty-2"
    ]
