"""Environment-backed application settings."""

from dataclasses import dataclass
import os
import json

from dotenv import load_dotenv


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
    certification_evidence_dir: str
    catalogue_import_max_bytes: int
    catalogue_import_max_rows: int
    catalogue_reachability_enabled: bool
    content_input_max_bytes: int = 10 * 1024 * 1024
    content_url_timeout_seconds: float = 8.0
    minio_public_endpoint: str = ""
    discourse_base_url: str = "http://127.0.0.1:3000"
    discourse_public_url: str = "http://127.0.0.1:3000"
    discourse_host_header: str = "localhost:3000"
    lemmy_local_test_destination_id: str = ""
    lemmy_local_test_base_url: str = "http://lemmy:8536"
    lemmy_local_test_community_id: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        # PowerShell launches do not get Compose's automatic .env injection.
        # Load the ignored local file without overriding explicit process env.
        load_dotenv(os.getenv("INCUBRIX_ENV_FILE", ".env"), override=False)
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
            certification_evidence_dir=os.getenv("CERTIFICATION_EVIDENCE_DIR", "artifacts/certification"),
            catalogue_import_max_bytes=int(os.getenv("CATALOGUE_IMPORT_MAX_BYTES", "2097152")),
            catalogue_import_max_rows=int(os.getenv("CATALOGUE_IMPORT_MAX_ROWS", "5000")),
            catalogue_reachability_enabled=os.getenv("CATALOGUE_REACHABILITY_ENABLED", "false").lower() == "true",
            content_input_max_bytes=int(os.getenv("CONTENT_INPUT_MAX_BYTES", str(10 * 1024 * 1024))),
            content_url_timeout_seconds=float(os.getenv("CONTENT_URL_TIMEOUT_SECONDS", "8")),
            minio_public_endpoint=os.getenv("MINIO_PUBLIC_ENDPOINT", ""),
            discourse_base_url=os.getenv("DISCOURSE_BASE_URL", "http://127.0.0.1:3000"),
            discourse_public_url=os.getenv("DISCOURSE_PUBLIC_URL", "http://127.0.0.1:3000"),
            discourse_host_header=os.getenv("DISCOURSE_HOST_HEADER", "localhost:3000"),
            lemmy_local_test_destination_id=os.getenv("LEMMY_LOCAL_TEST_DESTINATION_ID", ""),
            lemmy_local_test_base_url=os.getenv("LEMMY_LOCAL_TEST_BASE_URL", "http://lemmy:8536"),
            lemmy_local_test_community_id=int(os.getenv("LEMMY_LOCAL_TEST_COMMUNITY_ID", "2")),
        )
