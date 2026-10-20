"""Dev.to Articles API connector, with a simulation endpoint for local tests."""

from typing import Any

from app.connectors.base import Connector
from app.connectors.common import request_with_backoff, validate_rendition
from app.connectors.http import PlatformHttpAdapter
from app.errors import AppError


class DevtoConnector(Connector):
    def __init__(self, manifest: dict[str, Any], adapter: PlatformHttpAdapter, *, simulation_mode: bool, mock_url: str):
        self.manifest, self.adapter = manifest, adapter
        self.simulation_mode, self.mock_url = simulation_mode, mock_url.rstrip("/")

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    def _base_url(self, destination: dict, connection: dict | None = None) -> str:
        connection = connection or {}
        if self.simulation_mode:
            return f"{self.mock_url}/platform/devto"
        if connection.get("local_test") and connection.get("base_url"):
            return connection["base_url"].rstrip("/")
        if destination.get("local_test") and destination.get("base_url"):
            return destination["base_url"].rstrip("/")
        return destination.get("base_url", "https://dev.to/api")

    @staticmethod
    def _headers(connection: dict) -> dict[str, str]:
        return {
            "api-key": connection.get("api_key", ""),
            "Accept": "application/vnd.forem.api-v1+json",
            "Content-Type": "application/json",
        }

    def publish(self, rendition, destination, connection, _idempotency_key):
        article = {"title": rendition.get("title", ""), "body_markdown": rendition.get("body", ""), "published": True}
        if rendition.get("canonical_url"):
            article["canonical_url"] = rendition["canonical_url"]
        response = request_with_backoff(
            self.adapter,
            "POST",
            f"{self._base_url(destination, connection)}/articles",
            headers=self._headers(connection),
            payload={"article": article},
            manifest=self.manifest,
        )
        created = self._article_from_response(response.body)
        return {
            "platform_post_id": str(created["id"]),
            "live_url": created.get("url") or created.get("canonical_url"),
            "status": "live" if created.get("published", True) else "draft",
            "username": (created.get("user") or {}).get("username") or created.get("username"),
        }

    def get_status(self, platform_post_id, connection):
        response = request_with_backoff(
            self.adapter,
            "GET",
            f"{self._base_url({}, connection)}/articles/{platform_post_id}",
            headers=self._headers(connection),
            payload=None,
            manifest=self.manifest,
        )
        article = self._article_from_response(response.body)
        if str(article["id"]) != str(platform_post_id):
            raise AppError("CERTIFICATION_FAILED", "DEV.to returned a different article than requested.", False)
        expected_username = connection.get("username")
        actual_username = (article.get("user") or {}).get("username") or article.get("username")
        if expected_username and actual_username and actual_username != expected_username:
            raise AppError("CERTIFICATION_FAILED", "DEV.to returned an article owned by a different user.", False)
        return {
            "status": "live" if article.get("published", True) else "draft",
            "live_url": article.get("url") or article.get("canonical_url"),
            "platform_post_id": str(article["id"]),
            "username": actual_username,
            "published": bool(article.get("published", True)),
        }

    def delete(self, platform_post_id, connection):
        response = request_with_backoff(
            self.adapter,
            "PUT",
            f"{self._base_url({}, connection)}/articles/{platform_post_id}",
            headers=self._headers(connection),
            payload={"article": {"published": False}},
            manifest=self.manifest,
        )
        if response.status_code not in {200, 204}:
            raise AppError("CERTIFICATION_FAILED", "DEV.to did not accept article cleanup.", False)
        verification = self.adapter.request(
            "GET",
            f"{self._base_url({}, connection)}/articles/{platform_post_id}",
            headers=self._headers(connection),
            json=None,
        )
        if verification.status_code == 404:
            return {
                "deleted": True,
                "deletion_verified": True,
                "cleanup": "unpublished",
                "platform_post_id": str(platform_post_id),
            }
        if verification.status_code == 200:
            article = self._article_from_response(verification.body)
            if str(article["id"]) == str(platform_post_id) and article.get("published") is False:
                return {
                    "deleted": True,
                    "deletion_verified": True,
                    "cleanup": "unpublished",
                    "platform_post_id": str(platform_post_id),
                }
        raise AppError("CERTIFICATION_FAILED", "DEV.to article cleanup was not verified.", False)

    @staticmethod
    def _article_from_response(body: dict) -> dict:
        article = body.get("article", body)
        if not isinstance(article, dict) or not article.get("id"):
            raise AppError("PLATFORM_UNAVAILABLE", "DEV.to returned no article resource.", True)
        return article
