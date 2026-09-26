from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
import stat
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import MAX_HISTORY, MAX_SCREEN, CreateWindow, Input, Target, Window, sanitize_ansi

MARKER = "kitty_remote_token"


class KittyError(Exception):
    pass


class InputError(KittyError):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status


class WindowCreateError(KittyError):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status


async def command(
    args: list[str], data: bytes | None = None, limit: int = MAX_SCREEN, call_timeout: float = 4
) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read(stream, maximum):
        result = bytearray()
        while chunk := await stream.read(min(65536, maximum + 1 - len(result))):
            result.extend(chunk)
            if len(result) > maximum:
                raise KittyError("Kitty 返回内容超出限制")
        return bytes(result)

    stdout_task = asyncio.create_task(read(proc.stdout, limit))
    stderr_task = asyncio.create_task(read(proc.stderr, 8192))

    async def exchange():
        if data is not None:
            proc.stdin.write(data)
            await proc.stdin.drain()
            proc.stdin.close()
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        await proc.wait()
        if proc.returncode:
            # Do not expose terminal data, paths or credentials from arbitrary stderr.
            raise KittyError("Kitty 控制调用失败")
        return stdout

    try:
        return await asyncio.wait_for(exchange(), call_timeout)
    except BaseException:
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        await proc.communicate()
        raise


def socket_identity(address: str) -> tuple:
    if not address.startswith("unix:/"):
        raise KittyError("只允许绝对路径 Unix Socket")
    path = Path(address[5:])
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise KittyError("Socket 类型或所有者不正确")
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(1)
        sock.connect(str(path))
        pid, uid, _ = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
    if uid != os.getuid():
        raise KittyError("Kitty 进程不属于当前用户")
    # Field 22: process starttime. comm may contain spaces or parentheses.
    start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    return info.st_dev, info.st_ino, info.st_ctime_ns, pid, start


@dataclass
class Record:
    window: Window
    token: str
    process_identity: tuple


@dataclass
class Instance:
    address: str
    identity: tuple
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    records: dict[int, Record] = field(default_factory=dict)
    seen_ids: set[int] = field(default_factory=set)


class Kitty:
    def __init__(
        self,
        device_id: str,
        epoch: str,
        addresses: list[str] | None = None,
        socket_glob: str | None = None,
        executable: str = "kitten",
    ):
        self.device_id, self.epoch = device_id, epoch
        self.addresses = addresses or []
        self.runtime_dir = os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        self.socket_glob = (
            socket_glob
            if socket_glob is not None
            else (None if addresses else f"{self.runtime_dir}/kitty-remote-*")
        )
        self.executable = executable
        self.instances: dict[str, Instance] = {}
        self.lock = asyncio.Lock()
        self.errors: list[str] = []

    def candidates(self) -> list[str]:
        import glob

        return sorted(
            set(
                self.addresses
                + ["unix:" + p for p in (glob.glob(self.socket_glob) if self.socket_glob else [])]
            )
        )

    async def call(
        self, address: str, *args: str, data: bytes | None = None, limit: int = MAX_SCREEN
    ) -> bytes:
        return await command(
            [self.executable, "@", "--to", address, "--use-password", "never", *args],
            data=data,
            limit=limit,
        )

    async def raw_windows(self, address: str) -> list[tuple[dict, dict, dict]]:
        raw = await self.call(address, "ls", limit=4 * 1024 * 1024)
        return [
            (ow, tab, w) for ow in json.loads(raw) for tab in ow["tabs"] for w in tab["windows"]
        ]

    @staticmethod
    def process_identity(w: dict) -> tuple:
        if not w.get("created_at"):
            raise KittyError("Kitty 缺少窗口创建时间，无法安全绑定")
        return w["id"], w["pid"], w["created_at"]

    def describe(self, inst: Instance, ow: dict, tab: dict, w: dict) -> Window:
        return Window(
            target=Target(
                device_id=self.device_id,
                agent_epoch=self.epoch,
                kitty_instance_id=inst.id,
                window_id=w["id"],
            ),
            title=w.get("title", "")[:512],
            cwd=w.get("cwd", "")[:2048],
            os_window_id=ow["id"],
            tab_id=tab["id"],
            rows=w["lines"],
            cols=w["columns"],
            alternate=bool(w.get("in_alternate_screen")),
        )

    async def refresh(self) -> list[Window]:
        async with self.lock:
            result: list[Window] = []
            alive = set()
            self.errors = []
            for address in self.candidates():
                try:
                    identity = await asyncio.to_thread(socket_identity, address)
                    inst = self.instances.get(address)
                    if not inst or inst.identity != identity:
                        inst = Instance(address, identity)
                        self.instances[address] = inst
                    rows = await self.raw_windows(address)
                    # Any identity/marker mismatch rotates the whole instance namespace.
                    if any(
                        (w["id"] in inst.seen_ids and w["id"] not in inst.records)
                        or (
                            w["id"] in inst.records
                            and (
                                self.process_identity(w) != inst.records[w["id"]].process_identity
                                or w.get("user_vars", {}).get(MARKER) != inst.records[w["id"]].token
                            )
                        )
                        for _, _, w in rows
                    ):
                        inst = Instance(address, identity)
                        self.instances[address] = inst
                    ids = {w["id"] for _, _, w in rows}
                    inst.records = {i: r for i, r in inst.records.items() if i in ids}
                    for ow, tab, w in rows:
                        record = inst.records.get(w["id"])
                        if not record:
                            token = secrets.token_hex(24)
                            before = self.process_identity(w)
                            await self.call(
                                address,
                                "set-user-vars",
                                "--match",
                                f"id:{w['id']}",
                                f"{MARKER}={token}",
                            )
                            fresh = next(
                                (
                                    item
                                    for item in await self.raw_windows(address)
                                    if item[2]["id"] == w["id"]
                                ),
                                None,
                            )
                            if not fresh or self.process_identity(fresh[2]) != before:
                                raise KittyError("绑定过程中窗口发生变化")
                            ow, tab, w = fresh
                            if w.get("user_vars", {}).get(MARKER) != token:
                                raise KittyError("无法确认窗口标记")
                            record = Record(self.describe(inst, ow, tab, w), token, before)
                            inst.records[w["id"]] = record
                            inst.seen_ids.add(w["id"])
                        record.window = self.describe(inst, ow, tab, w)
                        result.append(record.window)
                    if await asyncio.to_thread(socket_identity, address) != identity:
                        raise KittyError("Kitty 实例在发现过程中发生变化")
                    alive.add(address)
                except (OSError, ValueError, KeyError, KittyError, TimeoutError):
                    self.errors.append("一个 Kitty 实例暂时不可用，请运行 doctor 检查")
                    if address in self.instances:
                        bad_id = self.instances[address].id
                        result = [w for w in result if w.target.kitty_instance_id != bad_id]
            self.instances = {a: i for a, i in self.instances.items() if a in alive}
            return result

    async def validate(self, target: Target) -> tuple[Instance, Record]:
        if target.device_id != self.device_id or target.agent_epoch != self.epoch:
            raise KittyError("目标属于旧代理或其他设备")
        inst = next((i for i in self.instances.values() if i.id == target.kitty_instance_id), None)
        if not inst or target.window_id not in inst.records:
            raise KittyError("目标窗口已失效")
        if await asyncio.to_thread(socket_identity, inst.address) != inst.identity:
            raise KittyError("Kitty 实例已变化")
        record = inst.records[target.window_id]
        row = next(
            (
                item
                for item in await self.raw_windows(inst.address)
                if item[2]["id"] == target.window_id
            ),
            None,
        )
        current = row[2] if row else None
        if (
            not current
            or self.process_identity(current) != record.process_identity
            or current.get("user_vars", {}).get(MARKER) != record.token
        ):
            raise KittyError("窗口身份或绑定标记已变化")
        record.window = self.describe(inst, *row)
        return inst, record

    @staticmethod
    def match(record: Record) -> str:
        return f"id:{record.window.target.window_id} and var:{MARKER}={record.token}"

    async def create_window(self, req: CreateWindow) -> tuple[Window, list[Window]]:
        submitted = False
        try:
            async with self.lock:
                if req.expires_at <= time.time():
                    raise WindowCreateError("rejected", "创建请求已过期")
                inst, record = await self.validate(req.target)
                if req.expires_at <= time.time():
                    raise WindowCreateError("rejected", "创建请求已过期")
                instance_id = inst.id
                try:
                    submitted = True
                    raw = await self.call(
                        inst.address,
                        "launch",
                        "--type=os-window",
                        "--source-window",
                        self.match(record),
                        "--cwd",
                        str(Path.home()),
                        limit=64,
                    )
                except (KittyError, OSError, ValueError, TimeoutError) as exc:
                    raise WindowCreateError(
                        "uncertain", "Kitty 调用结果不确定，请检查窗口列表后再操作"
                    ) from exc
            try:
                window_id = int(raw.strip())
                if window_id <= 0:
                    raise ValueError("无效窗口 ID")
            except ValueError as exc:
                raise WindowCreateError("uncertain", "Kitty 未返回有效窗口 ID") from exc
            for attempt in range(4):
                windows = await self.refresh()
                created = next(
                    (
                        w
                        for w in windows
                        if w.target.kitty_instance_id == instance_id
                        and w.target.window_id == window_id
                        and w.target != req.target
                    ),
                    None,
                )
                if created:
                    return created, windows
                if attempt < 3:
                    await asyncio.sleep(0.25)
            raise WindowCreateError("uncertain", "新窗口尚未确认，请检查窗口列表后再操作")
        except WindowCreateError:
            raise
        except (KittyError, OSError, ValueError, TimeoutError) as exc:
            if submitted:
                raise WindowCreateError(
                    "uncertain", "新窗口状态无法确认，请检查窗口列表后再操作"
                ) from exc
            raise WindowCreateError("rejected", "目标窗口不可用，未创建新窗口") from exc

    async def screen(self, target: Target, history: bool = False) -> tuple[Window, str]:
        async with self.lock:
            inst, record = await self.validate(target)
            args = [
                "get-text",
                "--match",
                self.match(record),
                "--extent",
                "all" if history else "screen",
                "--ansi",
            ]
            if not history:
                args.append("--add-cursor")
            dimensions = (record.window.rows, record.window.cols, record.window.alternate)
            data = await self.call(
                inst.address, *args, limit=MAX_HISTORY if history else MAX_SCREEN
            )
            await self.validate(target)
            if dimensions != (record.window.rows, record.window.cols, record.window.alternate):
                data = await self.call(
                    inst.address, *args, limit=MAX_HISTORY if history else MAX_SCREEN
                )
                await self.validate(target)
            text = data.decode("utf-8", "strict")
            return record.window, sanitize_ansi(text)

    async def execute(self, req: Input):
        submitted = False
        try:
            async with self.lock:
                if req.expires_at <= time.time():
                    raise InputError("rejected", "输入已过期")
                inst, record = await self.validate(req.target)
                if req.op != "keys":
                    if req.expires_at <= time.time():
                        raise InputError("rejected", "输入已过期")
                    submitted = True
                    await self.call(
                        inst.address,
                        "send-text",
                        "--match",
                        self.match(record),
                        "--stdin",
                        "--bracketed-paste=auto",
                        data=req.text.encode(),
                    )
                    if req.op == "text_enter":
                        await asyncio.sleep(0.15)
                keys = (
                    req.keys if req.op == "keys" else (["enter"] if req.op == "text_enter" else [])
                )
                for key in keys:
                    if req.expires_at <= time.time():
                        raise KittyError("输入在发送过程中到期")
                    inst, record = await self.validate(req.target)
                    if req.expires_at <= time.time():
                        raise KittyError("输入在校验过程中到期")
                    submitted = True
                    await self.call(inst.address, "send-key", "--match", self.match(record), key)
        except InputError:
            raise
        except (KittyError, OSError, ValueError, TimeoutError) as e:
            raise InputError(
                "uncertain" if submitted else "rejected",
                "部分发送或调用结果无法确认" if submitted else "目标不可用，未提交输入",
            ) from e
