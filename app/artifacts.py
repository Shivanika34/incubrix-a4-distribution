"""S3-compatible artifact storage and safe content-input handling."""

from __future__ import annotations

import hashlib
import ipaddress
import io
import json
import socket
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from httpcore._backends.sync import SyncBackend
from minio import Minio

from app.errors import AppError

ALLOWED_CONTENT_TYPES = {
    "application/json": ".json",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
}
PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".test", ".home.arpa")
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class S3ArtifactStore:
    """Minimal storage adapter backed by the configured MinIO-compatible endpoint."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, public_endpoint: str = ""):
        parsed = urlsplit(endpoint)
        self.bucket = bucket
        self.public_endpoint = public_endpoint
        self._access_key = access_key
        self._secret_key = secret_key

        def make_client(value: str, region: str | None = None):
            config = urlsplit(value)
            return Minio(
                config.netloc or config.path,
                access_key=access_key or None,
                secret_key=secret_key or None,
                secure=config.scheme == "https",
                region=region,
            )
        self.client = make_client(endpoint)
        self.signer = self.client

    def put_bytes(self, key: str, data: bytes, content_type: str) -> dict:
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
            self.client.put_object(
                self.bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
            signer = self.signer
            if self.public_endpoint:
                # Resolve the bucket region through the service-side endpoint.
                # MinIO otherwise probes the signer's public endpoint for its
                # region, which is often unreachable from inside the API container.
                region = self.client._get_region(self.bucket) or "us-east-1"
                public_config = urlsplit(self.public_endpoint)
                signer = Minio(
                    public_config.netloc or public_config.path,
                    access_key=self._access_key or None,
                    secret_key=self._secret_key or None,
                    secure=public_config.scheme == "https",
                    region=region,
                )
            download_url = signer.presigned_get_object(
                self.bucket, key, expires=timedelta(hours=1)
            )
        except Exception as error:
            raise AppError(
                "ARTIFACT_STORAGE_FAILED",
                "The artifact could not be saved to configured object storage.",
                True,
            ) from error
        return {
            "uri": f"s3://{self.bucket}/{key}",
            "download_url": download_url,
            "content_type": content_type,
            "size_bytes": len(data),
        }


class EphemeralArtifactStore:
    """Temporary local artifact storage for the single-service public demo."""

    def __init__(self, root: str, public_base_url: str = ""):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise AppError("ARTIFACT_NOT_FOUND", "The requested artifact does not exist.", False)
        return path

    def put_bytes(self, key: str, data: bytes, content_type: str) -> dict:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        quoted_key = quote(key, safe="/")
        base = self.public_base_url
        return {
            "uri": f"ephemeral://{quoted_key}",
            "download_url": f"{base}/api/v1/demo-artifacts/{quoted_key}",
            "content_type": content_type,
            "size_bytes": len(data),
        }

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        path = self._path(key)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise AppError("ARTIFACT_NOT_FOUND", "The requested artifact does not exist.", False) from error
        suffix_types = {".json": "application/json", ".md": "text/markdown", ".txt": "text/plain"}
        return data, suffix_types.get(path.suffix.lower(), "application/octet-stream")


class SafeURLFetcher:
    """Fetch public HTTP(S) content without automatic redirects or proxy use."""

    def __init__(self, *, timeout_seconds: float = 8.0, max_redirects: int = 3):
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects

    class _PublicOnlyNetworkBackend(SyncBackend):
        """Pin each socket connection to a resolved public IP to close DNS-rebinding gaps."""

        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            try:
                answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                addresses = []
                for answer in answers:
                    address = ipaddress.ip_address(answer[4][0].split("%", 1)[0])
                    if not address.is_global:
                        raise OSError("URL resolved to a non-public network address")
                    if address not in addresses:
                        addresses.append(address)
                if not addresses:
                    raise OSError("URL did not resolve to a public network address")
                return super().connect_tcp(
                    str(addresses[0]), port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (OSError, ValueError) as error:
                raise OSError("URL resolved to an unsafe network address") from error

    @staticmethod
    def validate_public_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError
            if parsed.username or parsed.password:
                raise ValueError
            host = parsed.hostname.rstrip(".").lower()
            if host == "localhost" or host.endswith(PRIVATE_HOST_SUFFIXES):
                raise ValueError
            try:
                addresses = [ipaddress.ip_address(host)]
            except ValueError:
                addresses = [
                    ipaddress.ip_address(item[4][0].split("%", 1)[0])
                    for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
                ]
            if not addresses or any(not address.is_global for address in addresses):
                raise ValueError
            return url
        except (ValueError, OSError, socket.gaierror) as error:
            raise AppError(
                "CONTENT_URL_UNSAFE",
                "The input URL must resolve only to a public HTTP or HTTPS destination.",
                False,
            ) from error

    def fetch(self, url: str, max_bytes: int) -> tuple[bytes, str, str]:
        current = self.validate_public_url(url)
        try:
            transport = httpx.HTTPTransport(trust_env=False)
            # HTTPX currently exposes no public resolver hook; replace its pool's
            # default backend with a backend that pins the socket to checked IPs.
            transport._pool._network_backend = self._PublicOnlyNetworkBackend()
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                for redirect_count in range(self.max_redirects + 1):
                    current = self.validate_public_url(current)
                    with client.stream("GET", current, headers={"Accept": ", ".join(ALLOWED_CONTENT_TYPES)}) as response:
                        if response.status_code in REDIRECT_STATUSES:
                            if redirect_count >= self.max_redirects:
                                raise AppError("CONTENT_URL_REDIRECT_LIMIT", "The input URL exceeded the redirect limit.", False)
                            location = response.headers.get("location")
                            if not location:
                                raise AppError("CONTENT_URL_FETCH_FAILED", "The input URL returned an invalid redirect.", False)
                            current = urljoin(current, location)
                            continue
                        if response.status_code < 200 or response.status_code >= 300:
                            raise AppError("CONTENT_URL_FETCH_FAILED", "The input URL could not be retrieved.", False, {"status_code": response.status_code})
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if content_type not in ALLOWED_CONTENT_TYPES:
                            raise AppError("CONTENT_INPUT_TYPE_UNSUPPORTED", "The input URL must return JSON, plain text, or Markdown.", False)
                        size_header = response.headers.get("content-length")
                        if size_header and int(size_header) > max_bytes:
                            raise AppError("CONTENT_INPUT_TOO_LARGE", "The input exceeds the configured content size limit.", False, {"max_bytes": max_bytes})
                        chunks = bytearray()
                        for chunk in response.iter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > max_bytes:
                                raise AppError("CONTENT_INPUT_TOO_LARGE", "The input exceeds the configured content size limit.", False, {"max_bytes": max_bytes})
                        return bytes(chunks), content_type, current
        except AppError:
            raise
        except Exception as error:
            raise AppError("CONTENT_URL_FETCH_FAILED", "The input URL could not be retrieved.", True) from error
        raise AppError("CONTENT_URL_FETCH_FAILED", "The input URL could not be retrieved.", False)


def parse_content_input(data: bytes, content_type: str, filename: str | None = None) -> dict:
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type not in ALLOWED_CONTENT_TYPES:
        raise AppError("CONTENT_INPUT_TYPE_UNSUPPORTED", "Supported content files are JSON, plain text, and Markdown.", False)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise AppError("CONTENT_INPUT_INVALID", "Content input must be valid UTF-8.", False) from error
    if normalized_type == "application/json":
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as error:
            raise AppError("CONTENT_INPUT_INVALID", "JSON content input is malformed.", False) from error
        content = decoded.get("content_item", decoded) if isinstance(decoded, dict) else None
        if not isinstance(content, dict) or not content:
            raise AppError("CONTENT_INPUT_INVALID", "JSON input must be a content object or contain a content_item object.", False)
        return content
    title = Path(filename or "Untitled").stem.strip() or "Untitled"
    return {"title": title[:255], "body": text}


def artifact_key(kind: str, filename: str | None = None, digest: str | None = None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in {".json", ".txt", ".md"}:
        suffix = ".json" if kind.endswith("json") else ".bin"
    if digest:
        return f"{kind}/{digest[:2]}/{digest}{suffix}"
    return f"{kind}/{time.strftime('%Y/%m/%d')}/{uuid4().hex}{suffix}"


def encode_json(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_manifest_settings(settings: dict) -> dict:
    secret_markers = ("secret", "token", "password", "api_key", "authorization", "credential")
    def clean(value):
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if any(marker in str(key).lower() for marker in secret_markers) else clean(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    return clean(settings)


def manifest_artifact_ref(artifact: dict) -> dict:
    """Keep durable location and integrity metadata, excluding bearer download URLs."""
    return {key: artifact[key] for key in ("uri", "content_type", "size_bytes", "sha256") if key in artifact}


def safe_source_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def store_orchestration_artifacts(
    store,
    *,
    job_id: str,
    request_body: dict,
    report: dict,
    settings: dict,
    connector_names: list[str],
    duration_ms: int,
) -> dict:
    """Persist a workflow result and its reproducibility manifest."""
    output_ref = store.put_bytes(
        artifact_key("orchestration", "result.json"),
        encode_json(report),
        "application/json",
    )
    content_item = request_body.get("content_item") or {}
    destinations = report.get("destinations", [])
    warnings = [warning for item in destinations for warning in item.get("warnings", [])]
    failures = [
        {
            "destination_id": item.get("destination_id"),
            "status": item.get("status"),
            "error_code": item.get("error_code"),
            "reason": item.get("reason"),
        }
        for item in destinations
        if item.get("status") in {"failed", "blocked", "skipped", "excluded", "outcome_unknown"}
    ]
    if report.get("error_code"):
        failures.append({"error_code": report["error_code"], "reason": report.get("message")})
    manifest = {
        "manifest_version": "1.0",
        "job_id": job_id,
        "inputs": {
            "content_ref": report.get("content_ref"),
            "artifacts": [manifest_artifact_ref(item) for item in request_body.get("input_artifacts", [])],
            "source_urls": [safe_source_url(item.get("source_url")) for item in request_body.get("input_artifacts", []) if item.get("source_url")],
            "source": {
                "url": safe_source_url(content_item.get("source_url")),
                "license": content_item.get("license"),
                "rights": content_item.get("rights"),
                "status": "provided" if any(content_item.get(key) for key in ("source_url", "license", "rights")) else "unknown",
            },
        },
        "outputs": {"result": manifest_artifact_ref(output_ref)},
        "providers_used": connector_names,
        "models_used": [],
        "model_usage_status": "not_applicable",
        "effective_settings": safe_manifest_settings({
            "dispatch": request_body.get("dispatch", False),
            "recommendation": request_body.get("recommendation"),
            "connections": request_body.get("connections", {}),
            "options": request_body.get("options", {}),
            **settings,
        }),
        "seed": {"value": None, "status": "not_applicable", "reason": "No stochastic model or seed was used."},
        "timings": {"duration_ms": duration_ms, "measurement": "monotonic wall-clock elapsed time"},
        "cost_estimate": {
            "amount": None,
            "currency": None,
            "status": "unknown",
            "reason": "No provider-cost metering adapter is configured, so cost was not measured.",
        },
        "warnings": warnings,
        "failures": failures,
    }
    manifest_ref = store.put_bytes(
        artifact_key("orchestration", "manifest.json"),
        encode_json(manifest),
        "application/json",
    )
    return {"result": output_ref, "manifest": manifest_ref}
