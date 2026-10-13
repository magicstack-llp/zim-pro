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
    # Making ZIMs: how many builds run at once, and how many threads each one uses.
    "max_concurrent_builds": 1,
    "build_workers": 4,
    "upload_limit_mb": 2048,
    # Fetching for builds and feeds: a proxy every request goes through (a VPN container's
    # HTTP proxy, say), and a FlareSolverr address for sites that put up a browser check.
    "build_proxy": "",
    "captcha_solver_url": "",
    # Feeds: ZIMs kept up to date from RSS. Their syncs have a queue of their own.
    "feed_auto_sync": True,
    "max_concurrent_feed_syncs": 1,
    "feed_default_interval": 60,
    "feed_default_keep": 0,
    # Remember the searches made on the Search page.
    "search_history": True,
    # Ask AI. Providers live in their own table; these pick the defaults among them.
    "ai_provider": "",
    "ai_model": "",
    "ai_max_sources": 6,
    # The user's own standing instructions, added to every prompt.
    "ai_instructions": "",
    # Internet search for questions the library cannot answer:
    # "off", "api" (a search service) or "model" (a model that searches by itself).
    "web_mode": "off",
    "web_service": "tavily",
    "web_api_key": "",
    "web_api_url": "",
    "web_provider": "",
    "web_model": "",
    # Reading answers aloud: "browser" (the browser's own voice), "provider" or "off".
    "tts_mode": "browser",
    "tts_provider": "",
    "tts_model": "",
    "tts_voice": "",
}

POLICIES = ("auto", "notify", "pinned")
