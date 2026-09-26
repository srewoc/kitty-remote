from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import random
import time
import uuid

from websockets.asyncio.client import connect

from .kitty import InputError, Kitty, KittyError, WindowCreateError
from .protocol import (
    MAX_MESSAGE,
    CreateWindow,
    Input,
    Outbox,
    Snapshot,
    Target,
    create_window_result,
    operation_result,
    packet,
)


def relay_clock_offset(server_time: float, elapsed: float, local_time: float) -> float:
    if not math.isfinite(server_time) or not 0 <= elapsed < 10:
        raise ValueError("中转时间同步失败")
    # The full handshake RTT is a conservative upper bound on return transit time.
    # Overestimating relay time expires input early rather than executing stale input.
    return server_time + elapsed - local_time


class Executor:
    def __init__(self, kitty: Kitty):
        self.kitty = kitty
        self.records: dict[str, tuple[str, dict, float]] = {}
        self.queues: dict[str, asyncio.Queue] = {}
        self.workers: dict[str, asyncio.Task] = {}

    def enqueue(self, req: Input | CreateWindow, emit):
        now = time.time()
        self.records = {k: v for k, v in self.records.items() if v[2] > now}
        fingerprint = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
        old = self.records.get(req.request_id)
        if old:
            emit(
                old[1]
                if old[0] == fingerprint
                else operation_result(req, "rejected", "重复 ID 内容不同")
            )
            return
        if req.expires_at <= now or req.expires_at > now + 11:
            emit(operation_result(req, "rejected", "请求已过期或有效期不合法"))
            return
        if len(self.records) >= 4096:
            emit(operation_result(req, "rejected", "去重记录已满，请稍后再试"))
            return
        key = req.target.key()
        queue = self.queues.get(key)
        if queue is None:
            if len(self.queues) >= 128:
                emit(operation_result(req, "rejected", "输入目标过多"))
                return
            queue = self.queues[key] = asyncio.Queue(16)
        if queue.full():
            emit(operation_result(req, "rejected", "输入队列已满"))
            return
        initial = operation_result(req, "received")
        self.records[req.request_id] = (fingerprint, initial, now + 60)
        queue.put_nowait((req, emit))
        if key not in self.workers or self.workers[key].done():
            self.workers[key] = asyncio.create_task(self.worker(key, queue))
        emit(initial)

    async def worker(self, key: str, queue: asyncio.Queue):
        try:
            while True:
                req, emit = await asyncio.wait_for(queue.get(), 15)
                try:
                    if isinstance(req, CreateWindow):
                        window, windows = await self.kitty.create_window(req)
                        emit(
                            packet(
                                "windows",
                                agent_epoch=self.kitty.epoch,
                                windows=[item.model_dump() for item in windows],
                            )
                        )
                        result = create_window_result(req, "created", window=window)
                    else:
                        await self.kitty.execute(req)
                        result = operation_result(req, "submitted")
                except (InputError, WindowCreateError) as e:
                    result = operation_result(req, e.status, str(e))
                except asyncio.CancelledError:
                    self.finish(req, operation_result(req, "uncertain", "连接中断"))
                    raise
                except Exception:
                    result = operation_result(req, "uncertain", "执行结果无法确认")
                self.finish(req, result)
                with contextlib.suppress(asyncio.QueueFull):
                    emit(result)
        except TimeoutError:
            pass
        finally:
            self.queues.pop(key, None)
            self.workers.pop(key, None)

    def finish(self, req: Input | CreateWindow, result: dict):
        old = self.records.get(req.request_id)
        if old:
            self.records[req.request_id] = (old[0], result, time.time() + 60)

    async def disconnect(self):
        workers = list(self.workers.values())
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.queues.clear()
        self.workers.clear()
        for rid, (fingerprint, result, expiry) in list(self.records.items()):
            if result["status"] == "received":
                self.records[rid] = (
                    fingerprint,
                    {**result, "status": "uncertain", "detail": "连接中断，旧输入未自动重发"},
                    expiry,
                )


class Agent:
    def __init__(
        self,
        relay: str,
        device_id: str,
        token: str,
        addresses: list[str] | None = None,
        socket_glob: str | None = None,
    ):
        self.relay, self.device_id, self.token = relay, device_id, token
        self.epoch = str(uuid.uuid4())
        self.kitty = Kitty(device_id, self.epoch, addresses, socket_glob)
        self.executor = Executor(self.kitty)
        self.seq = 0

    async def run(self):
        delay = 1
        while True:
            try:
                async with connect(
                    self.relay,
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    max_size=MAX_MESSAGE,
                    max_queue=16,
                    ping_interval=20,
                    open_timeout=10,
                ) as ws:
                    delay = 1
                    print("代理已连接中转", flush=True)
                    await self.session(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                print("代理连接中断，正在重连；旧输入不会补发", flush=True)
            finally:
                await self.executor.disconnect()
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 30)

    async def session(self, ws):
        out = Outbox()
        subscriptions: dict[str, dict] = {}
        windows = await self.kitty.refresh()
        started = time.monotonic()
        await ws.send(
            json.dumps(
                packet("hello", agent_epoch=self.epoch, windows=[w.model_dump() for w in windows])
            )
        )
        welcome = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if welcome.get("v") != 1 or welcome.get("type") != "welcome":
            raise ValueError("缺少中转时间握手")
        offset = relay_clock_offset(
            float(welcome["server_time"]), time.monotonic() - started, time.time()
        )

        async def writer():
            while True:
                await ws.send(json.dumps(await out.get(), ensure_ascii=False))

        async def poll():
            next_discovery = 0.0
            previous = ""
            while True:
                now = time.monotonic()
                if now >= next_discovery:
                    values = [w.model_dump() for w in await self.kitty.refresh()]
                    serialized = json.dumps(values, sort_keys=True)
                    if serialized != previous:
                        out.put(packet("windows", agent_epoch=self.epoch, windows=values))
                        previous = serialized
                    next_discovery = time.monotonic() + 2
                for key, sub in list(subscriptions.items()):
                    if sub["due"] > time.monotonic():
                        continue
                    try:
                        w, content = await self.kitty.screen(sub["target"])
                        signature = (content, w.rows, w.cols, w.alternate)
                        if signature != sub["last"]:
                            self.seq += 1
                            out.put(
                                Snapshot(
                                    type="snapshot",
                                    target=w.target,
                                    seq=self.seq,
                                    sampled_at=time.time(),
                                    rows=w.rows,
                                    cols=w.cols,
                                    alternate=w.alternate,
                                    content=content,
                                ).model_dump()
                            )
                            sub["last"] = signature
                            sub["idle"] = 0
                        else:
                            sub["idle"] += 1
                        sub["due"] = time.monotonic() + (2 if sub["idle"] >= 10 else 0.3)
                        if sub.get("buffer") and time.monotonic() >= sub.get("buffer_due", 0):
                            sub["buffer_due"] = time.monotonic() + 1
                            try:
                                bw, body = await self.kitty.screen(sub["target"], history=True)
                                signature = (body, bw.rows, bw.cols, bw.alternate)
                                if signature != sub.get("buffer_last"):
                                    self.seq += 1
                                    out.put(
                                        Snapshot(
                                            type="buffer",
                                            target=bw.target,
                                            seq=self.seq,
                                            sampled_at=time.time(),
                                            rows=bw.rows,
                                            cols=bw.cols,
                                            alternate=bw.alternate,
                                            content=body,
                                        ).model_dump()
                                    )
                                    sub["buffer_last"] = signature
                            except (KittyError, OSError, ValueError, TimeoutError):
                                if not sub.get("buffer_failed"):
                                    out.put(
                                        packet(
                                            "history_error",
                                            target=sub["target"].model_dump(),
                                            detail=(
                                                "历史读取失败或超过 16 MiB；"
                                                "实时画面仍可查看，点重新同步重试"
                                            ),
                                        )
                                    )
                                sub["buffer_failed"] = True
                                sub["buffer_last"] = None
                            else:
                                sub["buffer_failed"] = False
                    except (KittyError, OSError, ValueError, TimeoutError):
                        out.put(
                            packet(
                                "target_error",
                                target=sub["target"].model_dump(),
                                detail="窗口不可读或内容超限，请刷新窗口列表",
                            )
                        )
                        subscriptions.pop(key, None)
                await asyncio.sleep(0.05)

        async def reader():
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("v") != 1:
                    raise ValueError("协议版本不支持")
                kind = msg.get("type")
                if kind in {"input", "create_window"}:
                    req = (Input if kind == "input" else CreateWindow).model_validate(msg)
                    self.executor.enqueue(
                        req.model_copy(update={"expires_at": req.expires_at - offset}), out.put
                    )
                elif kind in {"subscribe", "unsubscribe", "history", "buffer_resync"}:
                    target = Target.model_validate(msg["target"])
                    key = target.key()
                    if kind == "subscribe":
                        if key not in subscriptions and len(subscriptions) >= 32:
                            continue
                        subscriptions[key] = {
                            "target": target,
                            "last": None,
                            "due": 0,
                            "idle": 0,
                            "buffer": msg.get("buffer") is True,
                        }
                    elif kind == "buffer_resync":
                        if key in subscriptions:
                            subscriptions[key].update(
                                buffer_last=None, buffer_due=0, due=0, buffer_failed=False
                            )
                            out.drop_target(key)
                    elif kind == "unsubscribe":
                        subscriptions.pop(key, None)
                        out.drop_target(key)
                    else:
                        try:
                            w, content = await self.kitty.screen(target, history=True)
                            self.seq += 1
                            out.put(
                                Snapshot(
                                    type="history",
                                    target=target,
                                    seq=self.seq,
                                    sampled_at=time.time(),
                                    rows=w.rows,
                                    cols=w.cols,
                                    alternate=w.alternate,
                                    content=content,
                                ).model_dump()
                            )
                        except (KittyError, OSError, ValueError, TimeoutError):
                            out.put(
                                packet(
                                    "history_error",
                                    target=target.model_dump(),
                                    detail="历史不可用或超过旧版单帧限制，请刷新网页使用连续历史",
                                )
                            )
                elif kind == "ping":
                    out.put(packet("pong"))
                else:
                    raise ValueError("未知消息")

        tasks = [asyncio.create_task(c()) for c in (writer, poll, reader)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
