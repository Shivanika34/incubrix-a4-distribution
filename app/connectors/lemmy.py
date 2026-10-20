"""Lemmy connector using the community post API."""

from typing import Any
from urllib.parse import urlsplit

from app.connectors.base import Connector
from app.connectors.common import request_with_backoff, validate_rendition
from app.connectors.http import PlatformHttpAdapter
from app.errors import AppError


class LemmyConnector(Connector):
    def __init__(
        self,
        manifest: dict[str, Any],
        adapter: PlatformHttpAdapter,
        *,
        simulation_mode: bool,
        mock_url: str,
        local_test_destination_id: str = "",
        local_test_base_url: str = "http://lemmy:8536",
        local_test_community_id: int = 2,
        local_mock_base_url: str = "http://lemmy-test:8080/platform",
    ):
        self.manifest, self.adapter = manifest, adapter
        self.simulation_mode, self.mock_url = simulation_mode, mock_url.rstrip("/")
        self.local_test_destination_id = local_test_destination_id
        self.local_test_base_url = local_test_base_url.rstrip("/")
        self.local_test_community_id = int(local_test_community_id)
        self.local_mock_base_url = local_mock_base_url.rstrip("/")

    @staticmethod
    def _local_test_requested(connection: dict) -> bool:
        return "local_test" in connection and connection.get("local_test") not in (False, None)

    @staticmethod
    def _matches_local_url(value: Any, expected: str) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed, target = urlsplit(value), urlsplit(expected)
            return (
                parsed.scheme == target.scheme == "http"
                and parsed.username is None
                and parsed.password is None
                and parsed.hostname == target.hostname
                and parsed.port == target.port
                and parsed.path.rstrip("/") == target.path.rstrip("/")
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False

    def local_test_kind(self, destination: dict, connection: dict) -> str | None:
        """Return the exact approved local route, or None for an unsafe override."""
        if connection.get("local_test") is not True:
            return None
        destination_id = destination.get("id") or connection.get("destination_id")
        destination_url = destination.get("url") or connection.get("destination_url")
        destination_key = destination.get("connector_key") or connection.get("connector_key")
        surface = destination.get("surface") or connection.get("surface")
        category = destination.get("category") or connection.get("community_id")
        community_id = connection.get("community_id") or category

        # Preserve the pre-existing disposable Lemmy mock service while only
        # allowing its exact internal URL and registered Lemmy destination.
        try:
            fixture_category = int(category)
            fixture_community = int(community_id)
        except (TypeError, ValueError):
            fixture_category = fixture_community = 0
        if (
            destination_id
            and destination_key == "lemmy"
            and surface == "community_post"
            and self._matches_local_url(destination_url, self.local_mock_base_url)
            and self._matches_local_url(connection.get("base_url"), self.local_mock_base_url)
            and fixture_category > 0
            and fixture_community == fixture_category
        ):
            return "local_mock_fixture"

        if not self.local_test_destination_id:
            return None
        if not self._matches_local_url(self.local_test_base_url, "http://lemmy:8536"):
            return None
        try:
            category_id = int(category)
            requested_community_id = int(community_id)
        except (TypeError, ValueError):
            return None
        real_match = (
            str(destination_id) == self.local_test_destination_id
            and destination_key == "lemmy"
            and surface == "community_post"
            and self._matches_local_url(destination_url, self.local_test_base_url)
            and self._matches_local_url(connection.get("base_url"), self.local_test_base_url)
            and category_id == self.local_test_community_id
            and requested_community_id == self.local_test_community_id
        )
        if real_match:
            return "local_real_instance"
        return None

    def is_approved_local_test(self, destination: dict, connection: dict) -> bool:
        return self.local_test_kind(destination, connection) is not None

    def validate(self, rendition, _destination, _connection):
        return validate_rendition(self.manifest, rendition)

    def _base_url(self, destination: dict, connection: dict) -> str:
        if self._local_test_requested(connection):
            if not self.is_approved_local_test(destination, connection):
                raise AppError(
                    "REQUEST_VALIDATION_ERROR",
                    "The local Lemmy override is restricted to its configured destination and internal URL.",
                    False,
                )
            kind = self.local_test_kind(destination, connection)
            if kind == "local_mock_fixture":
                return self.local_mock_base_url
            return self.local_test_base_url
        if self.simulation_mode:
            return f"{self.mock_url}/platform/lemmy"
        return (connection.get("base_url") or destination.get("base_url") or destination["url"]).rstrip("/")

    @staticmethod
    def _community_id(destination: dict, connection: dict) -> int:
        value = connection.get("community_id") or destination.get("category")
        try:
            community_id = int(value)
        except (TypeError, ValueError):
            raise AppError("DEST_SPEC_VIOLATION", "Lemmy community_id must be a positive integer.", False)
        if community_id <= 0:
            raise AppError("DEST_SPEC_VIOLATION", "Lemmy community_id must be a positive integer.", False)
        return community_id

    def _auth_token(self, connection: dict, base_url: str, *, required: bool) -> str | None:
        if not required:
            return None
        if connection.get("auth"):
            return str(connection["auth"])
        username = connection.get("username") or connection.get("username_or_email")
        password = connection.get("password")
        if not username or not password:
            return None
        response = request_with_backoff(
            self.adapter,
            "POST",
            f"{base_url}/api/v3/user/login",
            headers={"Content-Type": "application/json"},
            payload={
                "username_or_email": username,
                "password": password,
                "stay_logged_in": False,
            },
            manifest=self.manifest,
        )
        token = response.body.get("jwt") or response.body.get("auth")
        if not token:
            raise AppError("CONNECTION_AUTH_EXPIRED", "Lemmy login returned no authentication token.", False)
        return str(token)

    @staticmethod
    def _headers(_connection: dict) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def publish(self, rendition, destination, connection, _idempotency_key):
        base_url = self._base_url(destination, connection)
        community_id = self._community_id(destination, connection)
        payload = {
            "name": rendition.get("title", ""),
            "body": rendition.get("body", ""),
            "community_id": community_id,
        }
        local_test_kind = self.local_test_kind(destination, connection) if self._local_test_requested(connection) else None
        token = self._auth_token(connection, base_url, required=local_test_kind == "local_real_instance" or not self.simulation_mode)
        if local_test_kind == "local_real_instance" and not token:
            raise AppError("CONNECTION_AUTH_EXPIRED", "Lemmy local-instance publishing requires an auth token or username and password.", False)
        if token:
            payload["auth"] = token
        response = request_with_backoff(
            self.adapter, "POST", f"{base_url}/api/v3/post",
            headers=self._headers(connection), payload=payload, manifest=self.manifest,
        )
        post = self._post_from_response(response.body)
        self._validate_community(post, community_id)
        post_id = str(post["id"])
        return {
            "platform_post_id": post_id,
            "live_url": self._live_url(post, base_url, post_id),
            "status": "live",
            "community_id": community_id,
        }

    def get_status(self, platform_post_id, connection, destination=None):
        destination = destination or {}
        base_url = self._base_url(destination, connection)
        response = request_with_backoff(
            self.adapter, "GET", f"{base_url}/api/v3/post?id={platform_post_id}",
            headers=self._headers(connection), payload=None, manifest=self.manifest,
        )
        post = self._post_from_response(response.body)
        expected_community = self._community_id(destination, connection)
        self._validate_identity(post, platform_post_id, expected_community)
        return {
            "status": "deleted" if post.get("deleted") else "live",
            "live_url": self._live_url(post, base_url, str(platform_post_id)),
            "platform_post_id": str(post["id"]),
            "community_id": expected_community,
            "deleted": bool(post.get("deleted")),
        }

    def delete(self, platform_post_id, connection):
        base_url = self._base_url({}, connection)
        local_test_kind = self.local_test_kind({}, connection) if self._local_test_requested(connection) else None
        token = self._auth_token(connection, base_url, required=local_test_kind == "local_real_instance" or not self.simulation_mode)
        if local_test_kind == "local_real_instance" and not token:
            raise AppError("CONNECTION_AUTH_EXPIRED", "Lemmy local-instance deletion requires an auth token or username and password.", False)
        try:
            post_id: int | str = int(platform_post_id)
        except (TypeError, ValueError):
            if not connection.get("local_test"):
                raise AppError("CERTIFICATION_FAILED", "Lemmy post ID must be numeric.", False)
            post_id = str(platform_post_id)
        payload = {"post_id": post_id, "deleted": True}
        if token:
            payload["auth"] = token
        request_with_backoff(
            self.adapter, "POST", f"{base_url}/api/v3/post/delete",
            headers=self._headers(connection), payload=payload, manifest=self.manifest,
        )
        verification = self.adapter.request(
            "GET",
            f"{base_url}/api/v3/post?id={platform_post_id}",
            headers=self._headers(connection),
            json=None,
        )
        if verification.status_code == 404:
            return {"deleted": True, "deletion_verified": True, "platform_post_id": str(platform_post_id)}
        if verification.status_code == 200:
            post = self._post_from_response(verification.body)
            expected_community = self._community_id({}, connection)
            self._validate_identity(post, platform_post_id, expected_community)
            if post.get("deleted") is True:
                return {"deleted": True, "deletion_verified": True, "platform_post_id": str(platform_post_id)}
        raise AppError("CERTIFICATION_FAILED", "Lemmy delete was not confirmed by a subsequent read.", False)

    @staticmethod
    def _post_from_response(body: dict) -> dict:
        post_view = body.get("post_view")
        if isinstance(post_view, dict) and isinstance(post_view.get("post"), dict):
            return {**post_view["post"], "community": post_view.get("community") or {}}
        post = body.get("post", body)
        if not isinstance(post, dict) or not post.get("id"):
            raise AppError("PLATFORM_UNAVAILABLE", "Lemmy returned no post resource.", True)
        return post

    @staticmethod
    def _validate_community(post: dict, community_id: int) -> None:
        post_community = post.get("community_id") or post.get("community", {}).get("id")
        if post_community is not None and int(post_community) != community_id:
            raise AppError("CERTIFICATION_FAILED", "Lemmy returned a post from the wrong community.", False)

    @classmethod
    def _validate_identity(cls, post: dict, platform_post_id: str, community_id: int) -> None:
        if str(post.get("id")) != str(platform_post_id):
            raise AppError("CERTIFICATION_FAILED", "Lemmy returned a different post than requested.", False)
        cls._validate_community(post, community_id)

    @staticmethod
    def _live_url(post: dict, base_url: str, post_id: str) -> str:
        return post.get("url") or post.get("local_url") or f"{base_url}/post/{post_id}"
