"""A dedicated raw terminal fixture. It never runs shell commands from input."""

import codecs
import json
import os
import sys
import termios
import threading
import time
import tty
from pathlib import Path

log = Path(sys.argv[1])
old = termios.tcgetattr(sys.stdin)
decoder = codecs.getincrementaldecoder("utf-8")("replace")
pending = ""
line = ""
pasting = False
lock = threading.Lock()
# Continuous redraws, like an agent CLI's spinner (main screen) or full-screen TUI (alternate).
# They run until STOP; the limit only ends a spinner left behind by a failed test.
SPIN_LIMIT_SECONDS = 60
SPIN_INTERVAL = 0.08
stop_spin = threading.Event()


def write(text):
    with lock:
        sys.stdout.write(text)
        sys.stdout.flush()


def spinning(end):
    return not stop_spin.is_set() and time.monotonic() < end


def spin():
    frame = 0
    end = time.monotonic() + SPIN_LIMIT_SECONDS
    while spinning(end):
        write(f"\r\x1b[2K\x1b[33m⠋ working {frame:03d} · 中文🙂\x1b[0m")
        frame += 1
        time.sleep(SPIN_INTERVAL)
    write("\r\x1b[2KSPIN DONE\r\n")


def altspin():
    frame = 0
    end = time.monotonic() + SPIN_LIMIT_SECONDS
    write("\x1b[?1049h")
    while spinning(end):
        rows = "".join(
            f"\x1b[38;2;{80 + n * 6};200;{255 - n * 6}mALTSPIN row {n:02d} frame {frame:03d} 中文"
            f"\x1b[0m\x1b[K\r\n"
            for n in range(26)
        )
        write("\x1b[H" + rows)
        frame += 1
        time.sleep(SPIN_INTERVAL)
    write("\x1b[?1049l\r\nALTSPIN DONE\r\n")


def enter():
    global line
    command = line.strip()
    if command == "ALT":
        write("\x1b[?1049h\x1b[2J\x1b[H\x1b[36mALTERNATE SCREEN 中文🙂\x1b[0m\r\n")
    elif command == "NORMAL":
        write("\x1b[?1049l\r\nMAIN SCREEN\r\n")
    elif command == "LINES":
        for n in range(70):
            write(f"history row {n:03d} 中文🙂\r\n")
    elif command in {"SPIN", "ALTSPIN"}:
        stop_spin.clear()
        threading.Thread(target=spin if command == "SPIN" else altspin, daemon=True).start()
    elif command == "STOP":
        stop_spin.set()
    elif command == "LARGE":
        for n in range(10000):
            write(f"large history {n:05d} 中文🙂 " + "0123456789" * 12 + "\r\n")
    else:
        write("\r\nRECEIVED: " + line.replace("\n", " / ") + "\r\n")
    line = ""


try:
    tty.setraw(sys.stdin.fileno())
    title = "KR acceptance A" if "input-a" in log.name else "KR acceptance B"
    write(f"\x1b]2;{title}\x07")
    write("\x1b[?2004h\x1b[2J\x1b[H\x1b[38;2;140;228;199mKITTY REMOTE · LIVE TEST\x1b[0m\r\n")
    write("READY — 中文🙂\r\nThis window is an isolated test fixture.\r\n")
    while True:
        data = os.read(sys.stdin.fileno(), 4096)
        if not data:
            break
        with log.open("a") as file:
            file.write(json.dumps({"bytes": data.hex()}) + "\n")
        pending += decoder.decode(data)
        while pending:
            if pending.startswith("\x1b[200~"):
                pending = pending[6:]
                pasting = True
                continue
            if pending.startswith("\x1b[201~"):
                pending = pending[6:]
                pasting = False
                continue
            if pending.startswith("\x1b") and len(pending) < 6:
                break
            ch, pending = pending[0], pending[1:]
            if ch == "\x03":
                line = ""
                write("\r\nINTERRUPTED\r\n")
            elif ch in "\r\n" and not pasting:
                enter()
            else:
                line += ch
                write(ch.replace("\n", "\r\n"))
finally:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
