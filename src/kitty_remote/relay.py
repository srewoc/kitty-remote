from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .buffer import BufferAssembler
from .protocol import (
    MAX_MESSAGE,
    CreateWindow,
    Input,
    Outbox,
    Snapshot,
    Target,
    Window,
    create_window_result,
    operation_result,
    packet,
    receipt,
)
from .settings import Settings
from .storage import Store


class RateLimit:
    def __init__(self):
        self.entries: dict[str, deque] = {}

    def allow(self, key: str, limit: int = 30, period: int = 60) -> bool:
        now = time.monotonic()
        if len(self.entries) > 4096:
            self.entries = {k: v for k, v in self.entries.items() if v and v[-1] > now - 300}
            if len(self.entries) > 4096:
                return False
        values = self.entries.setdefault(key, deque())
        while values and values[0] <= now - period:
            values.popleft()
        if len(values) >= limit:
            return False
        values.append(now)
        return True


@dataclass(eq=False)
class Browser:
    ws: WebSocket
    token: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    out: Outbox = field(default_factory=Outbox)
    target: Target | None = None
    last_seen: float = field(default_factory=time.time)
    buffer: bool = False


@dataclass(eq=False)
class AgentPeer:
    ws: WebSocket
    device_id: str
    epoch: str
    windows: dict[str, Window]
    out: Outbox = field(default_factory=Outbox)
    last_seen: float = field(default_factory=time.time)
    buffers: dict[str, BufferAssembler] = field(default_factory=dict)


class Hub:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.browsers: dict[str, Browser] = {}
        self.agents: dict[str, AgentPeer] = {}
        self.leases: dict[str, tuple[str, float]] = {}
        self.pending: dict[tuple[str, str, str], tuple[Input | CreateWindow, set[str], float]] = {}
        self.requests: dict[tuple[str, str, str], tuple[str, Input | CreateWindow, float]] = {}
        self.histories: dict[str, set[str]] = defaultdict(set)
        self.rate = RateLimit()
        self.close_tasks: set[asyncio.Task] = set()

    def emit(self, peer, msg: dict):
        try:
            peer.out.put(msg)
        except (asyncio.QueueFull, ValueError):
            task = asyncio.create_task(peer.ws.close(code=1013))
            self.close_tasks.add(task)
            task.add_done_callback(self.close_tasks.discard)

    def state(self) -> dict:
        devices = []
        for device in self.store.devices():
            if device["revoked"]:
                continue
            agent = self.agents.get(device["id"])
            devices.append(
                {
                    "id": device["id"],
                    "name": device["name"],
                    "online": bool(agent),
                    "windows": [w.model_dump() for w in agent.windows.values()] if agent else [],
                }
            )
        return packet("state", devices=devices, server_time=time.time())

    def broadcast(self):
        msg = self.state()
        for peer in list(self.browsers.values()):
            self.emit(peer, msg)

    def target_agent(self, target: Target) -> AgentPeer:
        agent = self.agents.get(target.device_id)
        if not agent or target.agent_epoch != agent.epoch or target.key() not in agent.windows:
            raise ValueError("设备离线或目标窗口已失效")
        return agent

    def lease_status(self, target: Target):
        key = target.key()
        lease = self.leases.get(key)
        if lease and lease[1] <= time.time():
            self.leases.pop(key, None)
            lease = None
        for peer in self.browsers.values():
            if peer.target == target:
                self.emit(
                    peer,
                    packet(
                        "lease",
                        target=target.model_dump(),
                        owned=bool(lease and lease[0] == peer.id),
                        available=lease is None,
                        expires_at=lease[1] if lease else 0,
                    ),
                )

    def claim(self, peer: Browser):
        if not peer.target:
            return
        self.target_agent(peer.target)
        key = peer.target.key()
        old = self.leases.get(key)
        if not old or old[1] <= time.time() or old[0] == peer.id:
            self.leases[key] = (peer.id, time.time() + self.settings.lease_seconds)
        self.lease_status(peer.target)

    def unsubscribe(self, peer: Browser):
        target, peer.target = peer.target, None
        peer.out.drop_snapshots()
        if not target:
            return
        key = target.key()
        if self.leases.get(key, (None,))[0] == peer.id:
            self.leases.pop(key, None)
        self.histories[key].discard(peer.id)
        if not self.histories[key]:
            self.histories.pop(key, None)
        if not any(p.target == target for p in self.browsers.values()):
            with contextlib.suppress(ValueError):
                self.target_agent(target).buffers.pop(key, None)
                self.emit(
                    self.target_agent(target), packet("unsubscribe", target=target.model_dump())
                )
        self.lease_status(target)

    def remove_browser(self, peer: Browser):
        self.browsers.pop(peer.id, None)
        self.unsubscribe(peer)
        for _, watchers, _ in self.pending.values():
            watchers.discard(peer.id)

    def remove_agent(self, agent: AgentPeer):
        if self.agents.get(agent.device_id) is not agent:
            return
        self.agents.pop(agent.device_id, None)
        for key, (req, watchers, _) in list(self.pending.items()):
            if key[0] == agent.device_id:
                for ident in watchers:
                    if ident in self.browsers:
                        self.emit(
                            self.browsers[ident],
                            operation_result(req, "uncertain", "设备连接中断"),
                        )
                self.pending.pop(key, None)
        for peer in list(self.browsers.values()):
            if peer.target and peer.target.device_id == agent.device_id:
                self.emit(
                    peer,
                    packet(
                        "target_error",
                        target=peer.target.model_dump(),
                        detail="设备离线，请在重连后重新选择窗口",
                    ),
                )
                self.unsubscribe(peer)
        self.broadcast()

    def browser_message(self, peer: Browser, msg: dict):
        peer.last_seen = time.time()
        if msg.get("v") != 1:
            raise ValueError("协议版本不支持")
        kind = msg.get("type")
        if kind == "ping":
            if peer.target:
                key = peer.target.key()
                lease = self.leases.get(key)
                if lease and lease[0] == peer.id and lease[1] > time.time():
                    self.leases[key] = (peer.id, time.time() + self.settings.lease_seconds)
            self.emit(peer, packet("pong"))
            return
        if not self.rate.allow("ws:" + peer.id, 120):
            raise ValueError("操作过于频繁，请稍后再试")
        if kind == "subscribe":
            target = Target.model_validate(msg["target"])
            agent = self.target_agent(target)
            self.unsubscribe(peer)
            peer.target = target
            peer.buffer = msg.get("buffer") is True
            self.emit(
                agent,
                packet(
                    "subscribe",
                    target=target.model_dump(),
                    buffer=any(p.buffer and p.target == target for p in self.browsers.values()),
                ),
            )
            self.claim(peer)
        elif kind == "buffer_resync":
            if not peer.target or not peer.buffer:
                raise ValueError("请先订阅连续历史")
            if not self.rate.allow("buffer_resync:" + peer.id, 12):
                raise ValueError("重新同步过于频繁")
            peer.out.drop_target(peer.target.key())
            agent = self.target_agent(peer.target)
            cached = agent.buffers.get(peer.target.key())
            if cached and cached.current:
                self.emit(peer, cached.current)
            self.emit(agent, packet("buffer_resync", target=peer.target.model_dump()))
        elif kind == "unsubscribe":
            self.unsubscribe(peer)
        elif kind == "claim":
            self.claim(peer)
        elif kind == "history":
            target = Target.model_validate(msg["target"])
            if peer.target != target:
                raise ValueError("请先订阅目标窗口")
            if not self.rate.allow("history:" + peer.id, 6):
                raise ValueError("历史读取过于频繁")
            self.histories[target.key()].add(peer.id)
            self.emit(self.target_agent(target), packet("history", target=target.model_dump()))
        elif kind in {"input", "create_window"}:
            req = (Input if kind == "input" else CreateWindow).model_validate(msg)
            try:
                agent = self.target_agent(req.target)
                lease = self.leases.get(req.target.key())
                if (
                    peer.target != req.target
                    or not lease
                    or lease[0] != peer.id
                    or lease[1] <= time.time()
                ):
                    raise ValueError("当前连接没有此窗口的控制权")
                if req.expires_at <= time.time():
                    raise ValueError("输入已过期")
                if len(self.pending) >= 4096:
                    raise ValueError("处理中请求过多")
                key = (req.target.device_id, req.target.agent_epoch, req.request_id)
                fingerprint = req.model_dump_json()
                known = self.requests.get(key)
                if known and known[2] > time.time():
                    if known[0] != fingerprint:
                        raise ValueError("重复 ID 内容不同")
                    req = known[1]
                else:
                    if isinstance(req, CreateWindow):
                        if not self.rate.allow("create_window:" + peer.id, 6):
                            raise ValueError("创建窗口过于频繁")
                        if len(agent.windows) >= 256:
                            raise ValueError("窗口数量已达上限")
                        if any(
                            isinstance(item[0], CreateWindow)
                            and item[0].target.device_id == req.target.device_id
                            for item in self.pending.values()
                        ):
                            raise ValueError("这台电脑已有创建请求正在处理")
                    self.requests = {k: v for k, v in self.requests.items() if v[2] > time.time()}
                    if len(self.requests) >= 4096:
                        raise ValueError("请求记录已满，请稍后再试")
                    req = req.model_copy(
                        update={"expires_at": min(req.expires_at, time.time() + 10)}
                    )
                    self.requests[key] = (fingerprint, req, time.time() + 60)
                old = self.pending.get(key)
                # put_nowait failure is known not to have reached the agent.
                agent.out.put(req.model_dump())
                if old:
                    old[1].add(peer.id)
                else:
                    self.pending[key] = (req, {peer.id}, time.time() + 15)
                self.emit(peer, operation_result(req, "received"))
            except (ValueError, asyncio.QueueFull) as e:
                self.emit(
                    peer,
                    operation_result(req, "rejected", str(e) or "代理队列已满"),
                )
        else:
            raise ValueError("未知操作")

    def windows(self, device_id: str, epoch: str, raw: list) -> dict[str, Window]:
        if len(raw) > 256:
            raise ValueError("窗口数超出限制")
        result = {}
        for item in raw:
            w = Window.model_validate(item)
            if w.target.device_id != device_id or w.target.agent_epoch != epoch:
                raise ValueError("窗口身份不属于当前代理")
            if w.target.key() in result:
                raise ValueError("重复窗口身份")
            result[w.target.key()] = w
        return result

    def agent_message(self, agent: AgentPeer, msg: dict):
        agent.last_seen = time.time()
        if msg.get("v") != 1:
            raise ValueError("协议版本不支持")
        kind = msg.get("type")
        if kind == "pong":
            return
        if kind == "windows":
            if msg.get("agent_epoch") != agent.epoch:
                raise ValueError("代理身份变化")
            agent.windows = self.windows(agent.device_id, agent.epoch, msg["windows"])
            for peer in list(self.browsers.values()):
                if (
                    peer.target
                    and peer.target.device_id == agent.device_id
                    and peer.target.key() not in agent.windows
                ):
                    self.emit(
                        peer,
                        packet(
                            "target_error",
                            target=peer.target.model_dump(),
                            detail="窗口已关闭或身份已变化",
                        ),
                    )
                    self.unsubscribe(peer)
            self.broadcast()
        elif kind in {"buffer_start", "buffer_chunk", "buffer_end"}:
            target = Target.model_validate(msg["target"])
            key = target.key()
            if target.device_id != agent.device_id or target.agent_epoch != agent.epoch:
                raise ValueError("历史目标不属于此代理")
            if key not in agent.windows or not any(
                p.buffer and p.target == target for p in self.browsers.values()
            ):
                return
            assembler = agent.buffers.setdefault(key, BufferAssembler())
            try:
                complete = assembler.push(msg)
            except ValueError:
                agent.buffers.pop(key, None)
                self.emit(agent, packet("buffer_resync", target=target.model_dump()))
                return
            if complete:
                for peer in self.browsers.values():
                    if peer.target == target and peer.buffer:
                        self.emit(peer, complete)
        elif kind == "create_window_result":
            target = Target.model_validate(msg["target"])
            key = (agent.device_id, agent.epoch, msg["request_id"])
            entry = self.pending.get(key)
            if (
                target.device_id != agent.device_id
                or target.agent_epoch != agent.epoch
                or not entry
                or not isinstance(entry[0], CreateWindow)
                or entry[0].target != target
            ):
                return
            status = msg.get("status")
            if status not in {"received", "created", "rejected", "uncertain"}:
                raise ValueError("创建结果状态无效")
            window = None
            if status == "created":
                window = Window.model_validate(msg["window"])
                if (
                    window.target.device_id != agent.device_id
                    or window.target.agent_epoch != agent.epoch
                    or window.target.kitty_instance_id != target.kitty_instance_id
                    or window.target == target
                    or agent.windows.get(window.target.key()) != window
                ):
                    raise ValueError("新窗口身份未确认")
            elif "window" in msg:
                raise ValueError("非成功结果不能携带新窗口")
            for ident in entry[1]:
                if ident in self.browsers:
                    self.emit(
                        self.browsers[ident],
                        create_window_result(
                            entry[0], status, str(msg.get("detail", ""))[:300], window
                        ),
                    )
            if status != "received":
                self.pending.pop(key, None)
        elif kind in {"snapshot", "history", "target_error", "history_error", "receipt"}:
            target = Target.model_validate(msg["target"])
            if target.device_id != agent.device_id or target.agent_epoch != agent.epoch:
                raise ValueError("响应目标不属于此代理")
            if kind == "receipt":
                key = (agent.device_id, agent.epoch, msg["request_id"])
                entry = self.pending.get(key)
                if not entry or entry[0].target != target:
                    return
                if msg.get("status") not in {
                    "received",
                    "submitted",
                    "rejected",
                    "failed",
                    "uncertain",
                }:
                    raise ValueError("回执状态无效")
                for ident in entry[1]:
                    if ident in self.browsers:
                        self.emit(
                            self.browsers[ident],
                            receipt(entry[0], msg["status"], str(msg.get("detail", ""))[:300]),
                        )
                if msg["status"] != "received":
                    self.pending.pop(key, None)
            else:
                if kind in {"snapshot", "history"}:
                    msg = Snapshot.model_validate(msg).model_dump()
                else:
                    msg = packet(
                        kind, target=target.model_dump(), detail=str(msg.get("detail", ""))[:300]
                    )
                waiters = (
                    self.histories.pop(target.key(), set())
                    if kind in {"history", "history_error"}
                    else None
                )
                for peer in list(self.browsers.values()):
                    if peer.target == target and (
                        waiters is None
                        or peer.id in waiters
                        or (kind == "history_error" and peer.buffer)
                    ):
                        self.emit(peer, msg)
        else:
            raise ValueError("未知代理消息")

    async def monitor(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for peer in list(self.browsers.values()):
                if not self.store.session(peer.token) or now - peer.last_seen > 45:
                    await peer.ws.close(code=4401)
                else:
                    self.emit(peer, packet("ping"))
            for agent in list(self.agents.values()):
                if now - agent.last_seen > 45:
                    await agent.ws.close(code=1013)
                else:
                    self.emit(agent, packet("ping"))
            for key, (_, expiry) in list(self.leases.items()):
                if expiry <= now:
                    self.leases.pop(key, None)
                    for peer in self.browsers.values():
                        if peer.target and peer.target.key() == key:
                            self.lease_status(peer.target)
                            break
            for key, (req, watchers, expiry) in list(self.pending.items()):
                if expiry <= now:
                    for ident in watchers:
                        if ident in self.browsers:
                            self.emit(
                                self.browsers[ident],
                                operation_result(req, "uncertain", "等待回执超时"),
                            )
                    self.pending.pop(key, None)


async def receive(ws: WebSocket) -> dict:
    raw = await ws.receive_text()
    if len(raw.encode()) > MAX_MESSAGE:
        raise ValueError("消息过大")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("消息必须是对象")
    return value


async def pump(peer, handler):
    async def writer():
        while True:
            await asyncio.wait_for(peer.ws.send_json(await peer.out.get()), 10)

    async def reader():
        while True:
            handler(await receive(peer.ws))

    tasks = [asyncio.create_task(writer()), asyncio.create_task(reader())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PairStart(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class PairCode(BaseModel):
    code: str = Field(min_length=8, max_length=12)
    pairing_id: str = ""


class PairClaim(BaseModel):
    pairing_id: str = Field(max_length=64)
    secret: str = Field(max_length=128)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.database)
    hub = Hub(store, settings)

    @asynccontextmanager
    async def lifespan(app):
        monitor = asyncio.create_task(hub.monitor())
        yield
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        for peer in [*hub.browsers.values(), *hub.agents.values()]:
            with contextlib.suppress(Exception):
                await peer.ws.close(code=1001)
        await asyncio.gather(*hub.close_tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub, app.state.store = hub, store

    @app.middleware("http")
    async def guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            machine = request.url.path in {"/api/pairings/start", "/api/pairings/claim"}
            if origin != settings.origin and (origin is not None or not machine):
                return JSONResponse({"detail": "请求来源不被允许"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH"}:
            # Bound the streamed body, including requests without Content-Length.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    def authenticated(request: Request):
        if not store.session(request.cookies.get("kr_session")):
            raise HTTPException(401, "请先登录")

    def limited(request: Request, action: str, limit=10):
        host = request.client.host if request.client else "unknown"
        if not hub.rate.allow(action + ":" + host, limit):
            raise HTTPException(429, "请求过于频繁，请稍后再试")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "protocol": 1}

    @app.post("/api/auth/login")
    async def login(body: LoginBody, request: Request, response: Response):
        limited(request, "login")
        token = await run_in_threadpool(
            store.login, body.username, body.password, settings.session_seconds
        )
        if not token:
            raise HTTPException(401, "账号或密码不正确")
        response.set_cookie(
            "kr_session",
            token,
            httponly=True,
            secure=not settings.dev,
            samesite="strict",
            max_age=settings.session_seconds,
            path="/",
        )
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request):
        authenticated(request)
        return {"authenticated": True}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        authenticated(request)
        token = request.cookies["kr_session"]
        store.logout(token)
        response.delete_cookie("kr_session", path="/")
        for peer in list(hub.browsers.values()):
            if peer.token == token:
                await peer.ws.close(code=4401)
                hub.remove_browser(peer)
        return {"ok": True}

    @app.post("/api/pairings/start")
    async def start_pairing(body: PairStart, request: Request):
        limited(request, "pair-start", 5)
        try:
            return store.start_pairing(body.name)
        except ValueError as e:
            raise HTTPException(429, str(e)) from e

    @app.post("/api/pairings/lookup")
    async def lookup(body: PairCode, request: Request):
        authenticated(request)
        limited(request, "pair-lookup", 10)
        found = store.lookup_pairing(body.code)
        if not found:
            raise HTTPException(404, "配对码无效或已过期")
        return found

    @app.post("/api/pairings/approve")
    async def approve(body: PairCode, request: Request):
        authenticated(request)
        limited(request, "pair-approve", 10)
        try:
            ident = store.approve_pairing(body.code, body.pairing_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        hub.broadcast()
        return {"device_id": ident}

    @app.post("/api/pairings/claim")
    async def claim(body: PairClaim, request: Request):
        limited(request, "pair-claim", 90)
        try:
            result = store.claim_pairing(body.pairing_id, body.secret)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return result or {"pending": True}

    @app.get("/api/devices")
    async def devices(request: Request):
        authenticated(request)
        return hub.state()

    @app.post("/api/devices/{device_id}/revoke")
    async def revoke(device_id: str, request: Request):
        authenticated(request)
        store.revoke(device_id)
        agent = hub.agents.get(device_id)
        if agent:
            await agent.ws.close(code=4403)
            hub.remove_agent(agent)
        hub.broadcast()
        return {"ok": True}

    @app.websocket("/ws/browser")
    async def browser_socket(ws: WebSocket):
        token = ws.cookies.get("kr_session", "")
        if ws.headers.get("origin") != settings.origin or not store.session(token):
            await ws.close(code=4401)
            return
        if len(hub.browsers) >= 32:
            await ws.close(code=1013)
            return
        await ws.accept()
        peer = Browser(ws, token)
        hub.browsers[peer.id] = peer
        hub.emit(peer, hub.state())

        def handle(msg):
            if not store.session(peer.token):
                raise ValueError("登录已失效")
            try:
                hub.browser_message(peer, msg)
            except (ValueError, KeyError, ValidationError):
                # Pydantic exception text may contain user input. Never echo it.
                hub.emit(peer, packet("error", detail="操作被拒绝，请检查目标、权限和消息格式"))

        try:
            await pump(peer, handle)
        except (WebSocketDisconnect, ValueError, RuntimeError, TimeoutError):
            pass
        finally:
            hub.remove_browser(peer)
            with contextlib.suppress(Exception):
                await ws.close()

    @app.websocket("/ws/agent")
    async def agent_socket(ws: WebSocket):
        auth = ws.headers.get("authorization", "")
        device_id = store.authenticate_device(auth[7:]) if auth.startswith("Bearer ") else None
        if not device_id:
            await ws.close(code=4401)
            return
        await ws.accept()
        peer = None
        try:
            hello = await asyncio.wait_for(receive(ws), 10)
            epoch = str(uuid.UUID(hello["agent_epoch"]))
            if hello.get("v") != 1 or hello.get("type") != "hello":
                raise ValueError("需要代理握手")
            if device_id in hub.agents:
                old = hub.agents[device_id]
                await old.ws.close(code=1012)
                hub.remove_agent(old)
            peer = AgentPeer(ws, device_id, epoch, hub.windows(device_id, epoch, hello["windows"]))
            hub.agents[device_id] = peer
            hub.emit(peer, packet("welcome", server_time=time.time()))
            hub.broadcast()

            def handle(msg):
                if store.authenticate_device(auth[7:]) != device_id:
                    raise ValueError("设备凭据已撤销")
                hub.agent_message(peer, msg)

            await pump(peer, handle)
        except (WebSocketDisconnect, ValueError, KeyError, RuntimeError, TimeoutError):
            pass
        finally:
            if peer:
                hub.remove_agent(peer)
            with contextlib.suppress(Exception):
                await ws.close()

    if (settings.web_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=settings.web_dir / "assets"), name="assets")

    @app.get("/")
    async def index():
        if not (settings.web_dir / "index.html").is_file():
            raise HTTPException(503, "请先构建 web 前端")
        return FileResponse(settings.web_dir / "index.html")

    return app
