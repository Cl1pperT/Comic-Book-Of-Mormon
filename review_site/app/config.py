"""Settings, read from environment variables (see README). Everything has a safe local default except in production."""
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path = HERE / "data"
    database_url: str = ""          # default: sqlite file inside data_dir
    secret_key: str = "dev-only-secret-change-me"
    tool_token: str = ""            # bearer token for the publish/pull tools; empty disables the tool API
    secure_cookies: bool = False    # True in production (HTTPS only)
    hold: timedelta = timedelta(hours=48)       # how long a claimed chapter is held, renewed on every save
    skip_limit: int = 3                         # skips allowed per skip_window
    skip_window: timedelta = timedelta(hours=24)
    max_issues_per_review: int = 300
    fast_seconds_per_page: int = 20             # reviews faster than this per page are flagged for editors
    site_name: str = "Book of Mormon Comic Review"
    extra: dict = field(default_factory=dict)

    @property
    def db_url(self):
        return self.database_url or f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"

    @property
    def media_dir(self):
        return self.data_dir / "media"


def load_settings():
    """Settings from the environment. Production (REVIEW_SECURE_COOKIES=1) refuses to start with the dev secret."""
    secure = os.environ.get("REVIEW_SECURE_COOKIES", "") == "1"
    secret = os.environ.get("REVIEW_SECRET_KEY", "")
    if secure and len(secret) < 32:
        raise RuntimeError("Set REVIEW_SECRET_KEY to a random string of at least 32 characters in production.")
    values = {"secure_cookies": secure}
    if secret:
        values["secret_key"] = secret
    if os.environ.get("REVIEW_DATA_DIR"):
        values["data_dir"] = Path(os.environ["REVIEW_DATA_DIR"]).resolve()
    if os.environ.get("REVIEW_DATABASE_URL"):
        values["database_url"] = os.environ["REVIEW_DATABASE_URL"]
    if os.environ.get("REVIEW_TOOL_TOKEN"):
        values["tool_token"] = os.environ["REVIEW_TOOL_TOKEN"]
    if os.environ.get("REVIEW_HOLD_HOURS"):
        values["hold"] = timedelta(hours=float(os.environ["REVIEW_HOLD_HOURS"]))
    return Settings(**values)
