from __future__ import annotations

import argparse
import asyncio
import fcntl
import getpass
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn

from .agent import Agent
from .kitty import Kitty, command, socket_identity
from .settings import Settings
from .storage import Store

DEFAULT_CREDENTIALS = Path.home() / ".config/kitty-remote/agent.json"


def relay_url(value: str, allow_http: bool = False) -> str:
    value = value.rstrip("/")
    Settings(Path("unused"), value, dev=allow_http)
    return value


def pair(args):
    base = relay_url(args.relay, args.allow_http)
    path = args.credentials.expanduser()
    if path.exists():
        raise ValueError("凭据文件已存在，请先撤销旧设备并移走旧文件")
    with httpx.Client(base_url=base, timeout=10, follow_redirects=False) as client:
        response = client.post("/api/pairings/start", json={"name": args.name})
        response.raise_for_status()
        pending = response.json()
        print(f"打开 {base} 登录，输入配对码：{pending['code']}", flush=True)
        while time.time() < pending["expires_at"]:
            result = client.post(
                "/api/pairings/claim",
                json={"pairing_id": pending["pairing_id"], "secret": pending["secret"]},
            )
            result.raise_for_status()
            data = result.json()
            if not data.get("pending"):
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as file:
                    json.dump({**data, "relay": base, "allow_http": args.allow_http}, file)
                print(f"配对完成，凭据已保存至 {path}（0600）")
                return
            time.sleep(2)
    raise ValueError("配对已过期，请重新发起")


async def doctor(args):
    adapter = Kitty(str(uuid.uuid4()), str(uuid.uuid4()), args.socket, args.socket_glob)
    for executable in ("kitty", "kitten"):
        print((await command([executable, "--version"])).decode().strip())
    success = 0
    for address in adapter.candidates():
        try:
            identity = await asyncio.to_thread(socket_identity, address)
            rows = await adapter.raw_windows(address)
            summary = []
            for _, _, w in rows:
                text = await adapter.call(
                    address,
                    "get-text",
                    "--match",
                    f"id:{w['id']}",
                    "--extent",
                    "screen",
                    "--ansi",
                    "--add-cursor",
                )
                summary.append(
                    {
                        "window_id": w["id"],
                        "rows": w["lines"],
                        "cols": w["columns"],
                        "bytes": len(text),
                        "identity_supported": bool(w.get("created_at")),
                    }
                )
            print(
                json.dumps(
                    {"socket": address, "kitty_pid": identity[3], "windows": summary},
                    ensure_ascii=False,
                )
            )
            success += 1
        except Exception:
            print(
                json.dumps(
                    {"socket": address, "error": "不可连接、取屏失败或缺少安全绑定信息"},
                    ensure_ascii=False,
                )
            )
    if not success:
        raise ValueError("未发现可用实例。检查 socket-only 与 listen_on 配置；不会自动重启 Kitty")
    print("只读检查完成；输入与目标应用的行为仍需专用窗口验收。")


def main():
    parser = argparse.ArgumentParser(description="Kitty Remote 私有终端遥控")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="创建唯一管理员")
    admin.add_argument(
        "--database", type=Path, default=Path(os.getenv("KR_DATABASE", ".state/relay.sqlite3"))
    )
    admin.add_argument("--username", default="admin")
    admin.add_argument("--password-stdin", action="store_true")
    pairing = sub.add_parser("pair", help="生成一次性配对码并领取凭据")
    pairing.add_argument("--relay", required=True)
    pairing.add_argument("--name", default=socket.gethostname())
    pairing.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    pairing.add_argument("--allow-http", action="store_true", help="仅用于本机 localhost 开发")
    relay = sub.add_parser("relay", help="运行单进程中转")
    relay.add_argument("--host", default="127.0.0.1")
    relay.add_argument("--port", type=int, default=8765)
    agent = sub.add_parser("agent", help="运行本机代理")
    agent.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    agent.add_argument("--socket", action="append", default=[])
    agent.add_argument("--socket-glob")
    diag = sub.add_parser("doctor", help="只读检查 Kitty 接入")
    diag.add_argument("--socket", action="append", default=[])
    diag.add_argument("--socket-glob")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "create-admin":
            password = (
                sys.stdin.readline().rstrip("\r\n")
                if args.password_stdin
                else getpass.getpass("管理员密码：")
            )
            Store(args.database).create_admin(args.username, password)
            print("管理员创建完成")
        elif args.command == "pair":
            pair(args)
        elif args.command == "doctor":
            asyncio.run(doctor(args))
        elif args.command == "relay":
            Settings.from_env()
            uvicorn.run(
                "kitty_remote.relay:create_app",
                factory=True,
                host=args.host,
                port=args.port,
                workers=1,
                ws_max_size=1024 * 1024,
                ws_max_queue=16,
                access_log=False,
                proxy_headers=False,
            )
        else:
            path = args.credentials.expanduser()
            info = path.lstat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or path.is_symlink():
                raise ValueError("凭据文件必须由当前用户拥有且权限为 0600，不能是符号链接")
            data = json.loads(path.read_text())
            base = relay_url(data["relay"], data.get("allow_http", False))
            u = urlsplit(base)
            ws_url = ("wss" if u.scheme == "https" else "ws") + "://" + u.netloc + "/ws/agent"
            with path.open("r") as credential_lock:
                try:
                    fcntl.flock(credential_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as e:
                    raise ValueError("该设备代理已在运行，请勿重复启动") from e
                asyncio.run(
                    Agent(
                        ws_url, data["device_id"], data["token"], args.socket, args.socket_glob
                    ).run()
                )

    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"操作失败：{type(e).__name__}。请检查配置、连接或已有账号。", file=sys.stderr)
        if isinstance(e, ValueError):
            print(str(e), file=sys.stderr)
        sys.exit(1)
