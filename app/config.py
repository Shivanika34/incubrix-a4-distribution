"""Environment-backed application settings."""

from dataclasses import dataclass
import os


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
        )
