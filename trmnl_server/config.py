"""Server configuration with YAML and environment variable support."""

from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from trmnl_server.config_loader import get_config_loader

# Load .env file if present
load_dotenv()


def _get_project_root() -> Path:
    """Get project root directory."""
    return Path(__file__).parent.parent


@dataclass
class ServerConfig:
    """Main server configuration."""

    # Server settings
    host: str = "0.0.0.0"
    port: int = 4567
    debug: bool = False
    log_level: str = "INFO"  # DEBUG surfaces per-request device/telemetry detail

    # Paths
    project_root: Path = field(default_factory=_get_project_root)

    # TRMNL device settings
    refresh_time: int = 120  # seconds between device refreshes
    setup_api_key: str = ""
    setup_friendly_id: str = "munich-glance"
    setup_message: str = "MunichGlance Dashboard"

    # Display settings
    dithering_mode: str = "none"  # none, floyd_steinberg, ordered

    # Timezone
    timezone: str = "Europe/Berlin"

    # Sleep mode settings
    sleep_enabled: bool = False
    sleep_start: str = "23:00"  # Time to start sleep (HH:MM)
    sleep_end: str = "06:30"  # Time to end sleep (HH:MM)
    sleep_image_path: str = ""  # Optional: path to custom sleep image (relative to web_dir)

    @property
    def var_dir(self) -> Path:
        """Runtime data directory."""
        return self.project_root / "var"

    @property
    def db_path(self) -> Path:
        """SQLite database path."""
        return self.var_dir / "db" / "trmnl.db"

    @property
    def generated_dir(self) -> Path:
        """Generated images directory."""
        return self.var_dir / "generated"

    @property
    def assets_dir(self) -> Path:
        """Static assets directory."""
        return self.project_root / "assets"

    @property
    def fonts_dir(self) -> Path:
        """Fonts directory."""
        return self.assets_dir / "fonts"

    @property
    def web_dir(self) -> Path:
        """Web static files directory."""
        return self.project_root / "web"

    def ensure_directories(self) -> None:
        """Create required directories if they don't exist."""
        self.var_dir.mkdir(parents=True, exist_ok=True)
        (self.var_dir / "db").mkdir(exist_ok=True)
        self.generated_dir.mkdir(exist_ok=True)

    @classmethod
    def from_yaml(cls, config_file: str | None = None) -> "ServerConfig":
        """Load configuration from YAML file with env var substitution."""
        loader = get_config_loader(config_file=config_file)

        # Parse debug as boolean
        debug_val = loader.get("server", "debug", False)
        if isinstance(debug_val, str):
            debug_val = debug_val.lower() in ("true", "1", "yes")

        # Parse sleep_enabled as boolean
        sleep_enabled_val = loader.get("device", "sleep_enabled", False)
        if isinstance(sleep_enabled_val, str):
            sleep_enabled_val = sleep_enabled_val.lower() in ("true", "1", "yes")

        return cls(
            host=loader.get("server", "host", "0.0.0.0"),
            port=int(loader.get("server", "port", 4567)),
            debug=debug_val,
            log_level=str(loader.get("server", "log_level", "INFO")),
            refresh_time=int(loader.get("device", "refresh_time", 120)),
            setup_api_key=loader.get("device", "api_key", ""),
            setup_friendly_id=loader.get("device", "friendly_id", "munich-glance"),
            setup_message=loader.get("device", "setup_message", "MunichGlance Dashboard"),
            dithering_mode=loader.get("display", "dithering_mode", "none"),
            timezone=loader.get("server", "timezone", "Europe/Berlin"),
            sleep_enabled=sleep_enabled_val,
            sleep_start=loader.get("device", "sleep_start", "23:00"),
            sleep_end=loader.get("device", "sleep_end", "06:30"),
            sleep_image_path=loader.get("device", "sleep_image_path", ""),
        )


# Global config instance
_config: ServerConfig | None = None


def get_config(config_file: str | None = None) -> ServerConfig:
    """Get or create the global config instance."""
    global _config
    if _config is None:
        _config = ServerConfig.from_yaml(config_file)
        _config.ensure_directories()
    return _config


def reload_config(config_file: str | None = None) -> ServerConfig:
    """Reload configuration from YAML."""
    global _config
    _config = ServerConfig.from_yaml(config_file)
    _config.ensure_directories()
    return _config
