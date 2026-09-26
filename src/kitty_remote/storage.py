from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

HASHER = PasswordHasher()
DUMMY = HASHER.hash(secrets.token_hex(16))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS admin (
                    id INTEGER PRIMARY KEY CHECK(id=1), name TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE,
                    revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pairings (
                    id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL,
                    secret_hash TEXT NOT NULL, name TEXT NOT NULL,
                    expires REAL NOT NULL, device_id TEXT, claimed INTEGER DEFAULT 0);
            """)
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_admin(self, name: str, password: str):
        if not name.strip() or len(name) > 64 or len(password) < 12:
            raise ValueError("账号不能为空，密码至少 12 个字符")
        with self.connect() as db:
            db.execute("INSERT INTO admin VALUES (1, ?, ?)", (name, HASHER.hash(password)))

    def login(self, name: str, password: str, lifetime: int) -> str | None:
        with self.connect() as db:
            user = db.execute("SELECT * FROM admin WHERE name=?", (name,)).fetchone()
            try:
                HASHER.verify(user["password"] if user else DUMMY, password)
            except VerificationError:
                return None
            if not user:
                return None
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?, ?)", (digest(token), time.time() + lifetime)
            )
            return token

    def session(self, token: str | None) -> bool:
        if not token:
            return False
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM sessions WHERE hash=? AND expires>?",
                    (digest(token), time.time()),
                ).fetchone()
                is not None
            )

    def logout(self, token: str):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (digest(token),))

    def start_pairing(self, name: str) -> dict:
        ident, secret = str(uuid.uuid4()), secrets.token_urlsafe(32)
        code = secrets.token_hex(4).upper()
        expires = time.time() + 300
        with self.connect() as db:
            db.execute("DELETE FROM pairings WHERE expires<=?", (time.time(),))
            if db.execute("SELECT count(*) FROM pairings").fetchone()[0] >= 100:
                raise ValueError("配对请求过多，请稍后重试")
            db.execute(
                "INSERT INTO pairings(id,code_hash,secret_hash,name,expires) VALUES(?,?,?,?,?)",
                (ident, digest(code), digest(secret), name, expires),
            )
        return {"pairing_id": ident, "secret": secret, "code": code, "expires_at": expires}

    def lookup_pairing(self, code: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id,name,expires,device_id FROM pairings "
                "WHERE code_hash=? AND expires>? AND claimed=0",
                (digest(code.strip().upper()), time.time()),
            ).fetchone()
            return dict(row) if row else None

    def approve_pairing(self, code: str, expected_id: str) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM pairings WHERE code_hash=? AND id=? AND expires>? AND claimed=0",
                (digest(code.strip().upper()), expected_id, time.time()),
            ).fetchone()
            if not row:
                raise ValueError("配对码无效或已过期")
            if row["device_id"]:
                return row["device_id"]
            device_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO devices(id,name,created) VALUES(?,?,?)",
                (device_id, row["name"], time.time()),
            )
            db.execute("UPDATE pairings SET device_id=? WHERE id=?", (device_id, row["id"]))
            return device_id

    def claim_pairing(self, ident: str, secret: str) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM pairings WHERE id=? AND secret_hash=? AND expires>? AND claimed=0",
                (ident, digest(secret), time.time()),
            ).fetchone()
            if not row:
                raise ValueError("配对请求无效或已领取")
            if not row["device_id"]:
                return None
            token = secrets.token_urlsafe(48)
            result = db.execute(
                "UPDATE devices SET token_hash=? WHERE id=? AND revoked=0",
                (digest(token), row["device_id"]),
            )
            if not result.rowcount:
                raise ValueError("设备已撤销")
            db.execute("UPDATE pairings SET claimed=1 WHERE id=?", (ident,))
            return {"device_id": row["device_id"], "token": token}

    def authenticate_device(self, token: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM devices WHERE token_hash=? AND revoked=0", (digest(token),)
            ).fetchone()
            return row["id"] if row else None

    def devices(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,name,revoked,created FROM devices")]

    def revoke(self, device_id: str):
        with self.connect() as db:
            db.execute("UPDATE devices SET revoked=1,token_hash=NULL WHERE id=?", (device_id,))
