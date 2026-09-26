"""Real browser -> relay -> agent -> isolated Kitty; never targets user windows.
Run from the project root with .venv/bin/python scripts/e2e.py.
Requires a graphical desktop, Kitty, a built frontend and Playwright Chromium.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from kitty_remote.buffer import BufferAssembler
from kitty_remote.storage import Store

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ADMIN_FILE = os.getenv("KR_E2E_ADMIN_FILE")
ARTIFACTS = ROOT / "artifacts" / ("public" if PUBLIC_ADMIN_FILE else ".")
ARTIFACTS.mkdir(parents=True, exist_ok=True)
processes: list[subprocess.Popen] = []
log_handles = []
results = {}
# Row lookups use real layout: first row whose box reaches below the terminal's top edge.
FIRST_ROW_JS = """el => {
  const top = el.getBoundingClientRect().top;
  const rows = el.querySelector('.terminal-lines').children;
  let lo = 0, hi = rows.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].getBoundingClientRect().bottom <= top + 1) lo = mid + 1; else hi = mid;
  }
  return rows[lo] ? rows[lo].textContent : '';
}"""
VISIBLE_TEXT_ROWS_JS = """el => {
  const box = el.getBoundingClientRect();
  return [...el.querySelectorAll('.term-line')].filter(row => {
    const r = row.getBoundingClientRect();
    return r.bottom > box.top && r.top < box.bottom && row.textContent.trim();
  }).length;
}"""
AT_BOTTOM_JS = "el => el.scrollTop + el.clientHeight >= el.scrollHeight - 2"


def start(args, env=None):
    handle = tempfile.TemporaryFile()
    log_handles.append(handle)
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=handle, stderr=handle)
    processes.append(proc)
    return proc


def wait_for(predicate, page=None, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        if page:
            page.wait_for_timeout(80)
        else:
            time.sleep(0.08)
    raise AssertionError("Timed out waiting for acceptance condition")


def raw_input(path):
    if not path.exists():
        return b""
    return b"".join(
        bytes.fromhex(json.loads(line)["bytes"]) for line in path.read_text().splitlines()
    )


try:
    with contextlib.nullcontext(tempfile.mkdtemp(prefix="kitty-remote-e2e-")) as temp:
        state = Path(temp)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        password = secrets.token_urlsafe(24)
        username = "admin"
        if PUBLIC_ADMIN_FILE:
            admin = json.loads(Path(PUBLIC_ADMIN_FILE).read_text())
            origin = admin["url"]
            assert origin.startswith("https://"), "Public acceptance requires HTTPS"
            username, password = admin["username"], admin["password"]
        else:
            Store(state / "relay.sqlite").create_admin(username, password)
            env = {
                **os.environ,
                "KR_DEV": "1",
                "KR_ORIGIN": origin,
                "KR_DATABASE": str(state / "relay.sqlite"),
                "KR_WEB_DIR": str(ROOT / "web/dist"),
            }
            start([str(ROOT / ".venv/bin/kitty-remote"), "relay", "--port", str(port)], env)
        with httpx.Client(base_url=origin, timeout=15) as client:

            def healthy():
                try:
                    return client.get("/api/health").status_code == 200
                except httpx.HTTPError:
                    return False

            wait_for(healthy)
            kitty = start(
                [
                    "kitty",
                    "--config",
                    "NONE",
                    "--listen-on",
                    f"unix:{state}/test-kitty",
                    "-o",
                    "allow_remote_control=socket-only",
                    "-o",
                    "confirm_os_window_close=0",
                    "-o",
                    "shell_integration=disabled",
                    "-o",
                    "scrollback_lines=30000",
                    "-o",
                    "initial_window_width=100c",
                    "-o",
                    "initial_window_height=28c",
                    "--title",
                    "KR acceptance A",
                    sys.executable,
                    str(ROOT / "scripts/tty_fixture.py"),
                    str(state / "input-a.jsonl"),
                ]
            )

            def sockets():
                return [p for p in state.glob("test-kitty*") if stat.S_ISSOCK(p.stat().st_mode)]

            wait_for(sockets)
            address = "unix:" + str(sockets()[0])
            subprocess.run(
                [
                    "kitten",
                    "@",
                    "--to",
                    address,
                    "launch",
                    "--type=tab",
                    "--title",
                    "KR acceptance B",
                    sys.executable,
                    str(ROOT / "scripts/tty_fixture.py"),
                    str(state / "input-b.jsonl"),
                ],
                check=True,
                capture_output=True,
                timeout=10,
            )
            pending = client.post("/api/pairings/start", json={"name": "Acceptance desktop"}).json()
            with sync_playwright() as p:
                executable = os.getenv("KR_CHROMIUM_PATH")
                if not executable and not Path(p.chromium.executable_path).exists():
                    candidates = sorted(
                        (Path.home() / ".cache/ms-playwright").glob(
                            "chromium-*/chrome-linux*/chrome"
                        )
                    )
                    executable = str(candidates[-1]) if candidates else None
                browser = p.chromium.launch(headless=True, executable_path=executable)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 920}, has_touch=True
                )
                page = context.new_page()
                page.add_init_script("""const NativeWS = window.WebSocket;
                    window.WebSocket = class extends NativeWS {
                      constructor(...args) { super(...args); window.__testSocket = this; }
                    };""")
                frames = []
                sent_frames = []
                buffers = {}
                buffer_receiver = {}

                def received(data):
                    message = json.loads(data)
                    frames.append(message)
                    if message.get("type") in {"buffer_start", "buffer_chunk", "buffer_end"}:
                        key = json.dumps(message["target"], sort_keys=True)
                        assembler = buffer_receiver.setdefault(key, BufferAssembler())
                        if message["type"] == "buffer_start" and message["base_seq"] == 0:
                            assembler = buffer_receiver[key] = BufferAssembler()
                        result = assembler.push(message)
                        if result:
                            buffers[key] = result

                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on(
                    "websocket",
                    lambda ws: (
                        ws.on("framereceived", received),
                        ws.on("framesent", lambda data: sent_frames.append(json.loads(data))),
                    ),
                )
                page.goto(origin)
                page.wait_for_load_state("networkidle")
                expect(page.get_by_role("heading", name="连接你的工作台")).to_be_visible()
                page.screenshot(path=str(ARTIFACTS / "login-desktop.png"), full_page=True)
                page.get_by_label("账号", exact=True).fill(username)
                page.get_by_label("密码", exact=True).fill(password)
                page.get_by_role("button", name="进入工作台 ↗").click()
                expect(page.get_by_role("heading", name="我的电脑")).to_be_visible()
                page.get_by_role("button", name="配对电脑", exact=True).first.click()
                page.get_by_label("配对码", exact=True).fill(pending["code"])
                page.get_by_role("button", name="查找电脑 ↗").click()
                expect(page.get_by_text("Acceptance desktop", exact=True)).to_be_visible()
                page.get_by_role("button", name="确认配对", exact=True).click()
                expect(page.get_by_role("status")).to_contain_text("已确认配对")
                page.get_by_role("button", name="关闭配对").click()
                credential = client.post(
                    "/api/pairings/claim",
                    json={"pairing_id": pending["pairing_id"], "secret": pending["secret"]},
                ).json()
                credentials_path = state / "agent.json"
                credentials_path.write_text(
                    json.dumps({**credential, "relay": origin, "allow_http": not PUBLIC_ADMIN_FILE})
                )
                credentials_path.chmod(0o600)
                start(
                    [
                        str(ROOT / ".venv/bin/kitty-remote"),
                        "agent",
                        "--credentials",
                        str(credentials_path),
                        "--socket",
                        address,
                    ]
                )
                expect(
                    page.get_by_role("button", name="KR acceptance A", exact=False)
                ).to_be_visible(timeout=20000)
                page.get_by_role("button", name="KR acceptance A", exact=False).click()
                expect(page.get_by_text("你正在控制", exact=True)).to_be_visible()
                wait_for(
                    lambda: any(
                        f.get("type") == "snapshot" and "READY" in f.get("content", "")
                        for f in frames
                    ),
                    page,
                )
                results["real_chain"] = True
                page.screenshot(path=str(ARTIFACTS / "console-desktop.png"), full_page=True)
                target = next(f["target"] for f in reversed(frames) if f.get("type") == "snapshot")
                field = page.get_by_label("任务输入")
                field.fill("中文🙂\n第二行")
                field.dispatch_event("compositionstart")
                expect(page.get_by_role("button", name="发送文字", exact=True)).to_be_disabled()
                field.dispatch_event("compositionend")
                page.get_by_role("button", name="发送文字", exact=True).click()
                expect(field).to_have_value("")
                wait_for(
                    lambda: "中文🙂\n第二行".encode() in raw_input(state / "input-a.jsonl"), page
                )
                assert b"\r" not in raw_input(state / "input-a.jsonl")
                page.get_by_role("button", name="Enter ↵", exact=True).click()
                wait_for(lambda: b"\r" in raw_input(state / "input-a.jsonl"), page)
                results["chinese_multiline_emoji_ime_enter"] = True
                unique = "dedup-" + secrets.token_hex(5)
                request = {
                    "v": 1,
                    "type": "input",
                    "request_id": str(uuid.uuid4()),
                    "target": target,
                    "op": "text",
                    "text": unique,
                    "keys": [],
                    "expires_at": next(
                        f["server_time"] for f in reversed(frames) if f.get("type") == "state"
                    )
                    + 60,
                }
                page.evaluate(
                    "m => { window.__testSocket.send(JSON.stringify(m)); "
                    "window.__testSocket.send(JSON.stringify(m)); }",
                    request,
                )
                wait_for(lambda: unique.encode() in raw_input(state / "input-a.jsonl"), page)
                wait_for(
                    lambda: any(
                        f.get("request_id") == request["request_id"]
                        and f.get("status") == "submitted"
                        for f in frames
                    ),
                    page,
                )
                assert raw_input(state / "input-a.jsonl").count(unique.encode()) == 1
                page.evaluate(
                    "m => window.__testSocket.send(JSON.stringify(m))",
                    {**request, "text": "CONFLICT"},
                )
                wait_for(
                    lambda: any(
                        f.get("request_id") == request["request_id"]
                        and f.get("status") == "rejected"
                        for f in frames
                    ),
                    page,
                )
                assert b"CONFLICT" not in raw_input(state / "input-a.jsonl")
                results["duplicate_and_conflicting_request"] = True
                page.get_by_role("button", name="Ctrl+C", exact=True).click()
                wait_for(lambda: any("INTERRUPTED" in f.get("content", "") for f in frames), page)
                field.fill("ALT")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                expect(field).to_have_value("")
                wait_for(
                    lambda: any(
                        f.get("alternate") and "ALTERNATE SCREEN" in f.get("content", "")
                        for f in frames
                    ),
                    page,
                )
                expect(page.get_by_test_id("terminal")).to_contain_text("ALTERNATE SCREEN")
                expect(page.get_by_test_id("terminal")).not_to_contain_text("READY")
                results["alternate_screen_and_interrupt"] = True
                results["full_snapshot_replacement"] = True
                field.fill("NORMAL")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                wait_for(lambda: any("MAIN SCREEN" in f.get("content", "") for f in frames), page)
                field.fill("LINES")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                wait_for(
                    lambda: any("history row 069" in f.get("content", "") for f in frames), page
                )
                wait_for(
                    lambda: any(
                        "history row 000" in f.get("content", "") for f in buffers.values()
                    ),
                    page,
                )
                results["history"] = True

                field.fill("LARGE")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                wait_for(
                    lambda: any("large history 09999" in b["content"] for b in buffers.values()),
                    page,
                    seconds=45,
                )
                expect(page.get_by_test_id("terminal")).to_contain_text(
                    "large history 09999", timeout=20000
                )
                full = next(b for b in buffers.values() if "large history 09999" in b["content"])
                assert "large history 00000" in full["content"]
                assert len(full["content"].encode()) > 1024 * 1024
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("button", name="固定布局", exact=True).click()
                expect(page.locator(".workspace")).to_have_class(
                    __import__("re").compile("is-pinned")
                )
                screen_box = page.locator(".screen-frame").bounding_box()
                assert screen_box and screen_box["height"] >= 470, screen_box
                results["pinned_screen_height_px"] = round(screen_box["height"])
                terminal = page.get_by_test_id("terminal")
                wait_for(lambda: terminal.evaluate(AT_BOTTOM_JS), page)
                touch = context.new_cdp_session(page)
                rect = terminal.bounding_box()
                assert rect
                center = {"x": rect["x"] + rect["width"] / 2, "y": rect["y"] + rect["height"] / 2}

                # Headless Chromium ignores touch-sourced synthesized scroll gestures, so drive
                # a real finger drag; positive distance moves the finger down (scrolls up).
                def swipe(distance, steps=15):
                    y0 = center["y"] - distance / 2
                    point = {"x": center["x"], "y": y0}
                    touch.send(
                        "Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]}
                    )
                    for step in range(1, steps + 1):
                        point = {"x": center["x"], "y": y0 + distance * step / steps}
                        touch.send(
                            "Input.dispatchTouchEvent",
                            {"type": "touchMove", "touchPoints": [point]},
                        )
                        page.wait_for_timeout(16)
                    touch.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                    page.wait_for_timeout(400)

                top_before_swipe = terminal.evaluate("el => el.scrollTop")
                swipe(400)
                assert terminal.evaluate("el => el.scrollTop") < top_before_swipe
                expect(page.get_by_role("button", name="回到最新 ↓", exact=True)).to_be_visible()
                results["native_touch_scroll"] = True
                history_requests = sum(
                    f.get("type") in {"history", "buffer_resync"} for f in sent_frames
                )
                terminal.evaluate("el => { el.scrollTop = 0; }")
                wait_for(lambda: terminal.evaluate(FIRST_ROW_JS).startswith("KITTY REMOTE"), page)
                field.fill("output-during-reading")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                expect(field).to_have_value("")
                expect(page.get_by_role("button", name="有新输出 ↓", exact=True)).to_be_visible(
                    timeout=15000
                )
                expect(terminal).to_contain_text("RECEIVED: output-during-reading")
                assert terminal.evaluate("el => el.scrollTop") == 0
                assert terminal.evaluate(FIRST_ROW_JS).startswith("KITTY REMOTE")
                results["reading_position_kept_on_output"] = True
                # Offset into the middle of a row, away from the 1 px edge tolerance.
                terminal.evaluate("el => { el.scrollTop = 40008; }")
                page.wait_for_timeout(300)
                anchor = terminal.evaluate(FIRST_ROW_JS)
                assert "large history" in anchor, anchor
                for label in ["增大字号", "减小字号"]:
                    page.get_by_role("button", name=label, exact=True).click()
                    page.wait_for_timeout(300)
                    assert terminal.evaluate(FIRST_ROW_JS) == anchor, (label, anchor)
                results["font_preserves_reading_anchor"] = True
                completed_buffers = sum(f.get("type") == "buffer_end" for f in frames)
                page.evaluate("window.__testSocket.close()")
                wait_for(
                    lambda: sum(f.get("type") == "buffer_end" for f in frames) > completed_buffers,
                    page,
                    seconds=45,
                )
                page.wait_for_timeout(500)
                assert terminal.evaluate(FIRST_ROW_JS) == anchor, anchor
                expect(page.get_by_text("你正在控制", exact=True)).to_be_visible()
                results["reading_position_kept_on_reconnect"] = True
                assert (
                    sum(f.get("type") in {"history", "buffer_resync"} for f in sent_frames)
                    == history_requests
                )
                assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
                for label in ["任务输入", "发送并回车 ↗"]:
                    node = (
                        page.get_by_label(label)
                        if label == "任务输入"
                        else page.get_by_role("button", name=label, exact=True)
                    )
                    box = node.bounding_box()
                    assert box and box["y"] >= 0 and box["y"] + box["height"] <= 844
                field.fill("unsent draft")
                page.get_by_role("button", name="切到按键", exact=True).click()
                expect(field).to_have_count(0)
                page.screenshot(path=str(ARTIFACTS / "console-mobile-keys.png"), full_page=True)
                page.get_by_role("button", name="更多按键 ⌨", exact=True).click()
                page.get_by_role("button", name="PageUp", exact=True).click()
                page.get_by_role("button", name="F12", exact=True).click()
                page.get_by_role("button", name="关闭", exact=True).click()
                page.get_by_role("button", name="Shift+Tab", exact=True).tap()
                wait_for(
                    lambda: all(
                        any(f.get("keys") == [k] for f in sent_frames)
                        for k in ["page_up", "f12", "shift+tab"]
                    ),
                    page,
                )
                wait_for(
                    lambda: all(
                        key in raw_input(state / "input-a.jsonl")
                        for key in [b"\x1b[5~", b"\x1b[24~", b"\x1b[Z"]
                    ),
                    page,
                )
                page.get_by_role("button", name="切到输入", exact=True).click()
                expect(field).to_have_value("unsent draft")
                field.fill("")
                page.screenshot(path=str(ARTIFACTS / "console-mobile-pinned.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 480})
                page.wait_for_timeout(300)
                box = page.get_by_role("button", name="发送并回车 ↗", exact=True).bounding_box()
                assert box and box["y"] >= 0 and box["y"] + box["height"] <= 480
                page.screenshot(
                    path=str(ARTIFACTS / "console-mobile-keyboard-height.png"), full_page=True
                )
                page.set_viewport_size({"width": 844, "height": 390})
                page.wait_for_timeout(300)
                assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
                page.screenshot(
                    path=str(ARTIFACTS / "console-mobile-landscape.png"), full_page=True
                )
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("button", name="有新输出 ↓", exact=True).click()
                wait_for(lambda: terminal.evaluate(AT_BOTTOM_JS), page)
                expect(page.locator(".jump-latest")).to_have_count(0)
                results["large_history_local_scroll"] = True
                field.fill("follow-check")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                # Earlier Shift+Tab bytes stay in the fixture's line and move its cursor back.
                expect(terminal).to_contain_text(
                    __import__("re").compile(r"RECEIVED\W*follow-check"), timeout=15000
                )
                page.wait_for_timeout(200)
                assert terminal.evaluate(AT_BOTTOM_JS)
                results["auto_follow_at_bottom"] = True

                # Continuous redraws while the reader scrolls: the row being read must not
                # jump and the viewport must never be left without text.
                terminal.evaluate("el => { el.scrollTop = el.scrollHeight / 2; }")
                page.wait_for_timeout(300)
                field.fill("SPIN")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                expect(terminal).to_contain_text("working", timeout=15000)
                anchor = terminal.evaluate(FIRST_ROW_JS)
                for _ in range(8):
                    page.wait_for_timeout(250)
                    assert terminal.evaluate(FIRST_ROW_JS) == anchor, anchor
                    assert terminal.evaluate(VISIBLE_TEXT_ROWS_JS) > 10
                for distance in [400, -450, 300]:
                    swipe(distance)
                    assert terminal.evaluate(VISIBLE_TEXT_ROWS_JS) > 10
                field.fill("STOP")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                expect(terminal).to_contain_text("SPIN DONE", timeout=15000)
                results["main_screen_redraw_while_scrolling"] = True
                page.locator(".jump-latest").click()
                for _ in range(8):
                    page.get_by_role("button", name="增大字号", exact=True).click()
                field.fill("ALTSPIN")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                expect(terminal).to_contain_text("ALTSPIN row 25", timeout=15000)
                terminal.evaluate("el => { el.scrollTop = 0; }")
                page.wait_for_timeout(300)
                for _ in range(8):
                    page.wait_for_timeout(250)
                    assert terminal.evaluate(FIRST_ROW_JS).startswith("ALTSPIN row 00")
                    assert terminal.evaluate(VISIBLE_TEXT_ROWS_JS) > 10
                for distance in [-400, 400, -400]:
                    swipe(distance)
                    assert terminal.evaluate(VISIBLE_TEXT_ROWS_JS) > 10
                field.fill("STOP")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                expect(terminal).to_contain_text("ALTSPIN DONE", timeout=15000)
                # Leaving the alternate screen shows the latest main-screen rows.
                wait_for(lambda: terminal.evaluate(AT_BOTTOM_JS), page)
                results["alternate_redraw_while_scrolling"] = True
                for _ in range(8):
                    page.get_by_role("button", name="减小字号", exact=True).click()
                touch.detach()
                results["pinned_layout"] = True
                results["extended_keys"] = True
                page.get_by_role("button", name="窗口", exact=True).click()
                page.get_by_role("button", name="KR acceptance B", exact=False).click()
                expect(page.get_by_text("你正在控制", exact=True)).to_be_visible()
                field.fill("second-window")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                wait_for(lambda: b"second-window" in raw_input(state / "input-b.jsonl"), page)
                assert b"second-window" not in raw_input(state / "input-a.jsonl")
                expect(page.get_by_test_id("terminal")).to_contain_text(
                    "second-window", timeout=15000
                )
                results["window_isolation"] = True
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(ARTIFACTS / "console-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                second = context.new_page()
                second.goto(origin)
                second.wait_for_load_state("networkidle")
                second.get_by_role("button", name="KR acceptance B", exact=False).click()
                expect(second.get_by_text("只读查看", exact=True)).to_be_visible()
                expect(second.get_by_role("button", name="另一连接正在控制")).to_be_disabled()
                results["single_controller"] = True
                second.close()
                previous_snapshots = sum(f.get("type") == "snapshot" for f in frames)
                page.evaluate("window.__testSocket.close()")
                wait_for(
                    lambda: sum(f.get("type") == "snapshot" for f in frames) > previous_snapshots,
                    page,
                )
                expect(page.get_by_text("你正在控制", exact=True)).to_be_visible()
                assert raw_input(state / "input-b.jsonl").count(b"second-window") == 1
                results["reconnect_without_replay"] = True
                page.get_by_role("button", name="窗口", exact=True).click()
                create_button = page.get_by_role("button", name="＋ 新建桌面窗口", exact=True)
                expect(create_button).to_be_enabled()
                page.screenshot(path=str(ARTIFACTS / "create-window-mobile.png"), full_page=True)
                create_button.click()
                expect(page.get_by_role("button", name="正在创建窗口…")).to_be_disabled()
                wait_for(lambda: any(f.get("type") == "create_window" for f in sent_frames), page)
                create_request = next(
                    f for f in reversed(sent_frames) if f.get("type") == "create_window"
                )
                source_state = next(f for f in reversed(frames) if f.get("type") == "state")
                source_window = next(
                    w
                    for d in source_state["devices"]
                    for w in d["windows"]
                    if w["target"] == create_request["target"]
                )
                wait_for(
                    lambda: any(
                        f.get("type") == "create_window_result"
                        and f.get("request_id") == create_request["request_id"]
                        and f.get("status") == "created"
                        for f in frames
                    ),
                    page,
                )
                created = next(
                    f
                    for f in reversed(frames)
                    if f.get("type") == "create_window_result"
                    and f.get("request_id") == create_request["request_id"]
                    and f.get("status") == "created"
                )["window"]
                assert created["os_window_id"] != source_window["os_window_id"]
                assert (
                    created["target"]["kitty_instance_id"]
                    == create_request["target"]["kitty_instance_id"]
                )
                wait_for(
                    lambda: any(
                        f.get("type") == "subscribe" and f.get("target") == created["target"]
                        for f in sent_frames
                    ),
                    page,
                )
                expect(page.get_by_text("你正在控制", exact=True)).to_be_visible()
                field.fill("pwd")
                page.get_by_role("button", name="发送并回车 ↗", exact=True).click()
                wait_for(
                    lambda: any(
                        f.get("type") == "snapshot"
                        and f.get("target") == created["target"]
                        and str(Path.home()) in f.get("content", "")
                        for f in frames
                    ),
                    page,
                )
                results["create_os_window_home_shell"] = True
                page.once("dialog", lambda d: d.accept())
                page.get_by_role("button", name="窗口", exact=True).click()
                page.locator("section.device").filter(
                    has=page.get_by_text("Acceptance desktop", exact=True)
                ).get_by_role("button", name="撤销设备", exact=True).click()
                expect(
                    page.get_by_role("button", name="KR acceptance B", exact=False)
                ).to_have_count(0)
                results["revocation"] = True
                page.get_by_role("button", name="退出", exact=True).click()
                expect(page.get_by_role("heading", name="连接你的工作台")).to_be_visible()
                page.screenshot(path=str(ARTIFACTS / "login-mobile.png"), full_page=True)
                assert not errors, errors
                results["browser_errors"] = errors
                results["android_device_tested"] = False
                results["public_network_tested"] = bool(PUBLIC_ADMIN_FILE)
                browser.close()
        (ARTIFACTS / "e2e-results.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(results, indent=2))
finally:
    for proc in reversed(processes):
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    if sys.exc_info()[0]:
        for handle in log_handles:
            handle.seek(0)
            print(handle.read().decode(errors="replace")[-3500:])
    for handle in log_handles:
        handle.close()

    if "temp" in globals():
        shutil.rmtree(temp)

    # A failed public test must not leave a test device authorized.
    if PUBLIC_ADMIN_FILE and "credential" in globals():
        with httpx.Client(base_url=origin, timeout=15, headers={"Origin": origin}) as cleanup:
            cleanup.post(
                "/api/auth/login", json={"username": username, "password": password}
            ).raise_for_status()
            cleanup.post(f"/api/devices/{credential['device_id']}/revoke").raise_for_status()
            cleanup.post("/api/auth/logout").raise_for_status()
