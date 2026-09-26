import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database: Path
    origin: str
    dev: bool = False
    web_dir: Path = Path("web/dist")
    session_seconds: int = 12 * 3600
    lease_seconds: int = 30

    def __post_init__(self):
        u = urlsplit(self.origin)
        if u.path or u.query or u.fragment or u.username or not u.hostname:
            raise ValueError("KR_ORIGIN 必须是没有路径的完整站点地址")
        if u.scheme != "https" and not (
            self.dev and u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("生产环境必须使用 HTTPS；开发 HTTP 仅允许 localhost")

    @classmethod
    def from_env(cls):
        return cls(
            database=Path(os.getenv("KR_DATABASE", ".state/relay.sqlite3")),
            origin=os.getenv("KR_ORIGIN", "http://127.0.0.1:8765"),
            dev=os.getenv("KR_DEV", "0") == "1",
            web_dir=Path(os.getenv("KR_WEB_DIR", "web/dist")),
        )
