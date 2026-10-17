"""The Appendix A connector contract. Connectors return data and never access our database."""

from abc import ABC, abstractmethod
from typing import Any


class Connector(ABC):
    @abstractmethod
    def validate(self, rendition: dict[str, Any], destination: dict[str, Any], connection: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def publish(self, rendition: dict[str, Any], destination: dict[str, Any], connection: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]: ...

    @abstractmethod
    def get_status(self, platform_post_id: str, connection: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def delete(self, platform_post_id: str, connection: dict[str, Any]) -> dict[str, Any]: ...

    def fetch_metrics(self, platform_post_id: str, connection: dict[str, Any], since: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def refresh_auth(self, connection: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def build_assist_pack(self, rendition: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
