from __future__ import annotations

import asyncio
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = 1
MAX_MESSAGE = 1024 * 1024
MAX_TEXT = 64 * 1024
MAX_SCREEN = 512 * 1024
MAX_HISTORY = 16 * 1024 * 1024
KEYS = frozenset(
    {
        "enter",
        "esc",
        "tab",
        "up",
        "down",
        "left",
        "right",
        "ctrl+c",
        "shift+tab",
        "backspace",
        "home",
        "end",
        "page_up",
        "page_down",
        "insert",
        "delete",
        "shift+enter",
        "ctrl+enter",
        "alt+b",
        "alt+f",
        "alt+backspace",
        *(f"ctrl+{key}" for key in "aeukwrldz"),
        *(f"f{number}" for number in range(1, 13)),
    }
)
ID = r"^[a-f0-9-]{36}$"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Target(Model):
    device_id: str = Field(pattern=ID)
    agent_epoch: str = Field(pattern=ID)
    kitty_instance_id: str = Field(pattern=ID)
    window_id: int = Field(gt=0)

    def key(self) -> str:
        return "/".join(map(str, self.model_dump().values()))


class Window(Model):
    target: Target
    title: str = Field(max_length=512)
    cwd: str = Field(max_length=2048)
    os_window_id: int
    tab_id: int
    rows: int = Field(ge=1, le=500)
    cols: int = Field(ge=1, le=1000)
    alternate: bool


class Input(Model):
    v: Literal[1] = 1
    type: Literal["input"] = "input"
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    target: Target
    op: Literal["text", "text_enter", "keys"]
    text: str = ""
    keys: list[str] = Field(default_factory=list, max_length=16)
    expires_at: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_payload(self):
        if len(self.text.encode()) > MAX_TEXT:
            raise ValueError("文字不能超过 64 KiB")
        if self.op == "keys":
            if self.text or not self.keys or any(k not in KEYS for k in self.keys):
                raise ValueError("按键不在允许列表内")
        elif self.keys or not self.text or "\x00" in self.text:
            raise ValueError("文字不能为空，也不能包含 NUL")
        return self


class CreateWindow(Model):
    v: Literal[1] = 1
    type: Literal["create_window"] = "create_window"
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    target: Target
    expires_at: float = Field(gt=0, allow_inf_nan=False)


class Snapshot(Model):
    v: Literal[1] = 1
    type: Literal["snapshot", "history", "buffer"]
    target: Target
    seq: int = Field(gt=0)
    sampled_at: float
    rows: int = Field(ge=1, le=500)
    cols: int = Field(ge=1, le=1000)
    alternate: bool
    content: str

    @model_validator(mode="after")
    def size(self):
        if len(self.content.encode()) > (MAX_HISTORY if self.type == "buffer" else MAX_SCREEN):
            raise ValueError("屏幕内容超出限制")
        return self


def packet(kind: str, **fields) -> dict:
    return {"v": VERSION, "type": kind, **fields}


def receipt(req: Input, status: str, detail: str = "") -> dict:
    return packet(
        "receipt",
        request_id=req.request_id,
        target=req.target.model_dump(),
        status=status,
        detail=detail,
    )


def create_window_result(
    req: CreateWindow, status: str, detail: str = "", window: Window | None = None
) -> dict:
    result = packet(
        "create_window_result",
        request_id=req.request_id,
        target=req.target.model_dump(),
        status=status,
        detail=detail,
    )
    if window is not None:
        result["window"] = window.model_dump()
    return result


def operation_result(req: Input | CreateWindow, status: str, detail: str = "") -> dict:
    if isinstance(req, CreateWindow):
        return create_window_result(req, status, detail)
    return receipt(req, status, detail)


def sanitize_ansi(text: str) -> str:
    """Allow display-only CSI; consume OSC/DCS/APC/PM payloads, including C1 forms."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b" or ch in "\x90\x98\x9b\x9d\x9e\x9f":
            if ch == "\x1b":
                i += 1
                if i >= len(text):
                    break
                kind = text[i]
            else:
                kind = {
                    "\x90": "P",
                    "\x98": "X",
                    "\x9b": "[",
                    "\x9d": "]",
                    "\x9e": "^",
                    "\x9f": "_",
                }[ch]
            i += 1
            if kind in "]PX^_":
                while i < len(text):
                    if text[i] in "\x07\x9c":
                        i += 1
                        break
                    if text.startswith("\x1b\\", i):
                        i += 2
                        break
                    i += 1
            elif kind == "[":
                start = i
                while i < len(text) and not ("@" <= text[i] <= "~"):
                    i += 1
                if i < len(text):
                    value = text[start : i + 1]
                    if re.fullmatch(r"(?:[0-9:;]*m|[0-9;]*[Hf]|[0-6] q|\?25[hl])", value):
                        out.append("\x1b[" + value)
                    i += 1
            continue
        if ch in "\n\r\t" or (ord(ch) >= 32 and not 127 <= ord(ch) <= 159):
            out.append(ch)
        i += 1
    return "".join(out)


class Outbox:
    """Reliable controls plus a latest screen and chunked buffer for each target."""

    def __init__(self, limit: int = 128):
        self.control: asyncio.Queue[dict] = asyncio.Queue(limit)
        self.latest: dict[str, dict] = {}
        self.ready = asyncio.Event()
        self.sent_buffers: dict[str, dict] = {}
        self.streaming = None
        self.streaming_key: str | None = None

    def put(self, message: dict):
        if message.get("type") == "buffer":
            message = Snapshot.model_validate(message).model_dump()
            key = "buffer:" + Target.model_validate(message["target"]).key()
            if key not in self.latest and len(self.latest) >= 64:
                raise asyncio.QueueFull
            self.latest[key] = message
            self.ready.set()
            return
        if len(json.dumps(message, ensure_ascii=False).encode()) > MAX_MESSAGE:
            raise ValueError("编码后的消息超过 1 MiB")
        if message.get("type") == "snapshot":
            key = Target.model_validate(message["target"]).key()
            if key not in self.latest and len(self.latest) >= 64:
                raise asyncio.QueueFull
            self.latest[key] = message
        else:
            self.control.put_nowait(message)
        self.ready.set()

    def drop_snapshots(self):
        self.latest.clear()
        self.sent_buffers.clear()
        self.streaming = None
        self.streaming_key = None

    def drop_target(self, key: str):
        for ident in (key, "buffer:" + key):
            self.latest.pop(ident, None)
            self.sent_buffers.pop(ident, None)
        if self.streaming_key in (key, "buffer:" + key):
            self.streaming = None
            self.streaming_key = None

    async def get(self) -> dict:
        while True:
            if not self.control.empty():
                return self.control.get_nowait()
            if self.streaming is not None:
                try:
                    return next(self.streaming)
                except StopIteration:
                    self.streaming = None
                    self.streaming_key = None
            if self.latest:
                key = next(iter(self.latest))
                message = self.latest.pop(key)
                if message.get("type") == "buffer":
                    from .buffer import frames

                    self.streaming = iter(frames(message, self.sent_buffers.get(key)))
                    self.streaming_key = key
                    self.sent_buffers[key] = message
                    continue
                return message
            self.ready.clear()
            await self.ready.wait()
