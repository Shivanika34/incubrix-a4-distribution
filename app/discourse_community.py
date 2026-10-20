"""Read-only public Discourse community API client."""

from urllib.parse import urlsplit
from html.parser import HTMLParser

import httpx

from app.errors import AppError


class _PlainTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag, _attrs):
        if tag in {"script", "style"}:
            self._ignored += 1
        elif not self._ignored and tag in {"p", "br", "li", "blockquote", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._ignored:
            self._ignored -= 1
        elif not self._ignored and tag in {"p", "li", "blockquote", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._ignored:
            self.parts.append(data)


def _plain_text(html: str) -> str:
    parser = _PlainTextParser()
    parser.feed(html)
    return " ".join("".join(parser.parts).split())


class DiscourseCommunityClient:
    """Fetch public community data without using publishing credentials."""

    def __init__(self, base_url: str, timeout_seconds: float = 8.0, transport=None, *, public_base_url: str | None = None, host_header: str | None = None):
        parsed = urlsplit(base_url.strip())
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "discourse"})
        ):
            raise ValueError("DISCOURSE_BASE_URL must be HTTPS or use a local Discourse address.")
        self.base_url = base_url.strip().rstrip("/")
        self.public_base_url = (public_base_url or self.base_url).strip().rstrip("/")
        self.host_header = host_header.strip() if host_header else None
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
                trust_env=False,
            ) as client:
                headers = {"Accept": "application/json"}
                if self.host_header:
                    headers["Host"] = self.host_header
                response = client.get(f"{self.base_url}{path}", params=params, headers=headers)
        except httpx.TimeoutException as error:
            raise AppError("PLATFORM_UNAVAILABLE", "The Discourse community did not respond in time.", True) from error
        except httpx.RequestError as error:
            raise AppError("PLATFORM_UNAVAILABLE", "The Discourse community could not be reached.", True) from error

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise AppError("PLATFORM_RATE_LIMIT", "The Discourse community rate limited this request.", True,
                           {"retry_after": retry_after} if retry_after else None)
        if response.status_code == 404:
            raise AppError("DISCOURSE_TOPIC_NOT_FOUND", "The requested Discourse topic was not found.", False)
        if response.is_redirect:
            raise AppError("PLATFORM_UNAVAILABLE", "The Discourse community returned an unsupported redirect.", True)
        if response.status_code >= 500:
            raise AppError("PLATFORM_UNAVAILABLE", "The Discourse community is temporarily unavailable.", True)
        if response.status_code >= 400:
            raise AppError("DISCOURSE_UPSTREAM_ERROR", "The Discourse community rejected the read request.", False,
                           {"status_code": response.status_code})
        try:
            body = response.json()
        except ValueError as error:
            raise AppError("PLATFORM_UNAVAILABLE", "The Discourse community returned invalid JSON.", True) from error
        if not isinstance(body, dict):
            raise AppError("PLATFORM_UNAVAILABLE", "The Discourse community returned an unexpected response.", True)
        return body

    def categories(self) -> list[dict]:
        body = self._get("/categories.json")
        category_list = body.get("category_list")
        categories = category_list.get("categories") if isinstance(category_list, dict) else None
        if not isinstance(categories, list):
            raise AppError("PLATFORM_UNAVAILABLE", "The Discourse categories response is incomplete.", True)
        return [
            {"id": item["id"], "name": item["name"], "slug": item.get("slug", ""),
             "topic_count": item.get("topic_count", 0), "description": item.get("description_text") or ""}
            for item in categories
            if isinstance(item, dict) and isinstance(item.get("id"), int) and isinstance(item.get("name"), str)
        ]

    def topics(self, per_page: int = 20) -> list[dict]:
        body = self._get("/latest.json", {"per_page": per_page})
        topic_list = body.get("topic_list")
        topics = topic_list.get("topics") if isinstance(topic_list, dict) else None
        if not isinstance(topics, list):
            raise AppError("PLATFORM_UNAVAILABLE", "The Discourse latest-topics response is incomplete.", True)
        users = {user.get("id"): user for user in body.get("users", []) if isinstance(user, dict)}
        return [self._topic_summary(item, users) for item in topics if isinstance(item, dict) and isinstance(item.get("id"), int)]

    def topic(self, topic_id: int) -> dict:
        body = self._get(f"/t/{topic_id}.json")
        if body.get("id") != topic_id:
            raise AppError("PLATFORM_UNAVAILABLE", "The Discourse topic response did not match the requested topic.", True)
        post_stream = body.get("post_stream")
        posts = post_stream.get("posts", []) if isinstance(post_stream, dict) else []
        return {
            "id": topic_id,
            "title": body.get("title") or body.get("fancy_title") or "",
            "slug": body.get("slug") or "",
            "category_id": body.get("category_id"),
            "reply_count": body.get("reply_count", max(0, body.get("posts_count", 1) - 1)),
            "posts_count": body.get("posts_count", len(posts)),
            "url": self._topic_url(body.get("slug", ""), topic_id),
            "posts": [
                {"id": post.get("id"), "post_number": post.get("post_number"),
                 "author": post.get("username") or post.get("name") or "Unknown",
                 "created_at": post.get("created_at"), "body": post.get("raw") or _plain_text(post.get("cooked") or "")}
                for post in posts if isinstance(post, dict)
            ],
        }

    @staticmethod
    def _topic_summary(topic: dict, users: dict) -> dict:
        posters = topic.get("posters") or []
        original = next((poster for poster in posters if isinstance(poster, dict) and "Original Poster" in poster.get("description", "")), None)
        author = users.get(original.get("user_id"), {}).get("username") if original else None
        return {
            "id": topic["id"], "title": topic.get("title") or topic.get("fancy_title") or "",
            "slug": topic.get("slug") or "", "category_id": topic.get("category_id"),
            "reply_count": topic.get("reply_count", max(0, topic.get("posts_count", 1) - 1)),
            "posts_count": topic.get("posts_count", 1), "author": author,
            "last_poster": topic.get("last_poster_username"),
            "url": DiscourseCommunityClient._topic_url(topic.get("slug", ""), topic["id"]),
            "excerpt": topic.get("excerpt") or "",
        }

    @staticmethod
    def _topic_url(slug: str, topic_id: int) -> str:
        # Slugs are supplied by the configured Discourse instance; do not accept
        # arbitrary links from its JSON as browser navigation targets.
        return f"/t/{slug}/{topic_id}" if slug else f"/t/{topic_id}"
