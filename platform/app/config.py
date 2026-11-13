import os
import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _local_config() -> dict:
    path = Path(os.getenv("APP_CONFIG_PATH", str(ROOT / "config.local.json"))).resolve()
    try:
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return {}


_FILE_CONFIG = _local_config()


def _value(name: str, default: str = "") -> str:
    return os.getenv(name, str(_FILE_CONFIG.get(name, default)))


@dataclass(frozen=True)
class Settings:
    data_source: str = os.getenv("DATA_SOURCE", "sqlite").lower()
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = int(os.getenv("APP_PORT", "8000"))
    events_source: str = _value("EVENTS_SOURCE", "sqlite").lower()
    database_path: Path = Path(_value("EVENTS_DB_PATH", str(ROOT / "data" / "events.sqlite3")))
    database_timeout: float = 0.25
    database_attempts: int = 3
    monitoring_source: str = _value("MONITORING_SOURCE", "sqlite").lower()
    monitoring_database_path: Path = Path(
        _value("MONITORING_DB_PATH", str(ROOT / "data" / "monitoring.sqlite3"))
    )
    response_source: str = _value("RESPONSE_SOURCE", "sqlite").lower()
    response_database_path: Path = Path(
        _value("RESPONSE_DB_PATH", _value("EVENTS_DB_PATH", str(ROOT / "data" / "events.sqlite3")))
    )
    credential_hmac_key: str = _value("CREDENTIAL_HMAC_KEY", "")
    connector_network_enabled: bool = _value("CONNECTOR_NETWORK_ENABLED", "false").lower() == "true"
    github_token: str = _value("GITHUB_TOKEN", "")
    github_api_base: str = _value("GITHUB_API_BASE", "https://api.github.com")
    github_repositories: tuple[str, ...] = ()
    gitee_token: str = _value("GITEE_TOKEN", "")
    gitee_api_base: str = _value("GITEE_API_BASE", "https://gitee.com/api/v5")
    gitee_repositories: tuple[str, ...] = ()
    weibo_access_token: str = _value("WEIBO_ACCESS_TOKEN", "")
    connector_timeout_seconds: float = float(_value("CONNECTOR_TIMEOUT_SECONDS", "8"))
    connector_max_files: int = int(_value("CONNECTOR_MAX_FILES", "50"))
    connector_max_file_bytes: int = int(_value("CONNECTOR_MAX_FILE_BYTES", str(512 * 1024)))
    connector_min_interval_seconds: float = float(_value("CONNECTOR_MIN_INTERVAL_SECONDS", "1"))
    connector_max_retries: int = int(_value("CONNECTOR_MAX_RETRIES", "2"))
    verification_network_enabled: bool = _value("VERIFICATION_NETWORK_ENABLED", "false").lower() == "true"
    verification_allowed_fingerprints: tuple[str, ...] = ()
    verification_timeout_seconds: float = float(_value("VERIFICATION_TIMEOUT_SECONDS", "8"))
    authorized_verification_endpoints: tuple[dict, ...] = ()
    dockerhub_username: str = _value("DOCKERHUB_USERNAME", "")
    controlled_verification_manifest_path: Path = Path(
        _value("CONTROLLED_VERIFICATION_MANIFEST", "")
    )
    connector_runs_path: Path = Path(_value("CONNECTOR_RUNS_DB_PATH", str(ROOT / "data" / "connector_runs.sqlite3")))
    settings_database_path: Path = Path(
        _value("SETTINGS_DB_PATH", str(ROOT / "data" / "settings.sqlite3"))
    )
    settings_master_key_path: Path = Path(
        _value("SETTINGS_MASTER_KEY_PATH", str(ROOT / "data" / "settings.master.key"))
    )
    local_operator_role: str = _value("APP_OPERATOR_ROLE", "admin").lower()


settings = Settings()
