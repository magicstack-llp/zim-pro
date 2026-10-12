from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Process configuration, read from KIWIX_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="KIWIX_", env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")
    state_dir: Path = Path("/state")
    host: str = "0.0.0.0"
    port: int = 8080
    admin_token: str | None = None
    catalog_url: str = "https://library.kiwix.org/catalog/v2/entries"
    web_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.state_dir / "kiwix-updater.sqlite3"


# Runtime settings live in the database and are editable from the UI.
DEFAULT_SETTINGS: dict[str, int | bool | str] = {
    "max_concurrent_downloads": 2,
    "connections_per_file": 4,
    "bandwidth_limit_kbps": 0,
    "catalog_refresh_hours": 24,
    "default_policy": "notify",
    "keep_old_versions": False,
    "mcp_allow_writes": False,
}

POLICIES = ("auto", "notify", "pinned")
