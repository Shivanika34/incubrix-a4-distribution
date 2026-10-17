"""Environment-backed application settings."""

from dataclasses import dataclass
import os
import json


@dataclass(frozen=True)
class Settings:
    database_url: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    log_level: str
    simulation_mode: bool
    default_rate_limit_requests: int
    default_rate_limit_window_seconds: int
    app_config_file: str
    callback_hmac_secret: str
    echo_sleep_seconds: float
    connectors_dir: str
    connector_disabled: frozenset[str]
    mock_platform_url: str
    webhook_secrets: dict[str, str]

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite://"),
            minio_endpoint=os.getenv("MINIO_ENDPOINT", "http://localhost:9000"),
            minio_access_key=os.getenv("MINIO_ACCESS_KEY", ""),
            minio_secret_key=os.getenv("MINIO_SECRET_KEY", ""),
            minio_bucket=os.getenv("MINIO_BUCKET", "incubrix-artifacts"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            simulation_mode=os.getenv("SIMULATION_MODE", "true").lower() == "true",
            default_rate_limit_requests=int(os.getenv("DEFAULT_RATE_LIMIT_REQUESTS", "60")),
            default_rate_limit_window_seconds=int(os.getenv("DEFAULT_RATE_LIMIT_WINDOW_SECONDS", "60")),
            app_config_file=os.getenv("APP_CONFIG_FILE", ""),
            callback_hmac_secret=os.getenv("CALLBACK_HMAC_SECRET", ""),
            echo_sleep_seconds=float(os.getenv("ECHO_SLEEP_SECONDS", "3")),
            connectors_dir=os.getenv("CONNECTORS_DIR", "connectors"),
            connector_disabled=frozenset(key.strip() for key in os.getenv("CONNECTOR_DISABLED", "").split(",") if key.strip()),
            mock_platform_url=os.getenv("MOCK_PLATFORM_URL", "http://localhost:8080"),
            webhook_secrets=json.loads(os.getenv("WEBHOOK_SECRETS_JSON", "{}")),
        )
