import asyncio
import json

import pytest
from test_core import target

from kitty_remote.buffer import BufferAssembler, frames
from kitty_remote.protocol import MAX_HISTORY, MAX_MESSAGE, Input, Outbox, Snapshot, packet


def buffer(content, seq=1, **kwargs):
    return Snapshot(
        type="buffer",
        target=target(),
        seq=seq,
        sampled_at=1,
        rows=24,
        cols=80,
        alternate=False,
        content=content,
        **kwargs,
    ).model_dump()


def apply(assembler, value, previous=None):
    result = None
    for part in frames(value, previous):
        assert len(json.dumps(part, ensure_ascii=False).encode()) < MAX_MESSAGE
        result = assembler.push(part)
    return result


@pytest.mark.parametrize(
    "before,after",
    [
        ("a\nb\nc", "a\n中文🙂\nc"),
        ("x\nx\nx", "x\nx"),
        ("a\nb\nc", "b\nc\nd"),
        ("", "hello"),
        ("hello", ""),
        ("a\nb\n", "a\nb\nc\n"),
        ("a", "a"),
        ("\n", "\n\n"),
    ],
)
def test_exact_splice(before, after):
    old = buffer(before)
    new = {**old, "seq": 2, "content": after}
    assembler = BufferAssembler()
    assert apply(assembler, old)["content"] == before
    assert apply(assembler, new, old)["content"] == after


def test_large_history_and_escape_expansion():
    original = buffer("\x1b[32m中文🙂\x1b[0m " * 60000 + "\n" * 4000)
    assert len(original["content"].encode()) > MAX_MESSAGE
    assert apply(BufferAssembler(), original) == original
    with pytest.raises(ValueError):
        buffer("a" * (MAX_HISTORY + 1))


def test_reject_broken_transfer_and_revision():
    old = buffer("a")
    new = {**old, "seq": 2, "content": "b"}
    with pytest.raises(ValueError, match="基础版本"):
        apply(BufferAssembler(), new, old)
    assembler = BufferAssembler()
    parts = list(frames(old, None))
    assembler.push(parts[0])
    with pytest.raises(ValueError, match="不完整"):
        assembler.push(parts[-1])
    assembler.push(parts[0])
    with pytest.raises(ValueError, match="顺序"):
        assembler.push({**parts[1], "index": 4})


async def test_latest_buffer_coalesces_with_receipt_priority():
    out = Outbox()
    first = buffer("a" * 300000)
    out.put(first)
    receiver = BufferAssembler()
    receiver.push(await out.get())
    out.put(packet("receipt", status="submitted"))
    assert (await out.get())["type"] == "receipt"
    second = {**first, "seq": 2, "content": "obsolete"}
    third = {**first, "seq": 3, "content": "latest 中文🙂"}
    out.put(second)
    out.put(third)
    results = []
    while len(results) < 2:
        result = receiver.push(await asyncio.wait_for(out.get(), 1))
        if result:
            results.append(result)
    assert results == [first, third]
    out.drop_target(target_from(first))
    out.put({**third, "seq": 4})
    assert (await out.get())["base_seq"] == 0


def target_from(value):
    from kitty_remote.protocol import Target

    return Target.model_validate(value["target"]).key()


@pytest.mark.parametrize(
    "key",
    ["shift+tab", "backspace", "page_up", "page_down", "ctrl+a", "alt+f", "f12", "shift+enter"],
)
def test_extended_key_allowlist(key):
    assert Input(
        request_id="test-keys", target=target(), op="keys", keys=[key], expires_at=1
    ).keys == [key]
