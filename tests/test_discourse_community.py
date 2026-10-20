"""Offline coverage for read-only Discourse community browsing."""

import httpx
import pytest
from starlette.requests import Request

from app.discourse_community import DiscourseCommunityClient
from app.errors import AppError


def _client(handler):
    return DiscourseCommunityClient("https://community.test", transport=httpx.MockTransport(handler))


def test_categories_and_latest_topics_follow_public_response_shapes():
    def handler(request):
        if request.url.path == "/categories.json":
            return httpx.Response(200, json={"category_list": {"categories": [
                {"id": 4, "name": "General", "slug": "general", "topic_count": 2, "description_text": "Talk"},
                {"id": "bad", "name": "Ignore malformed item"},
            ]}})
        assert request.url.path == "/latest.json"
        assert request.url.params["per_page"] == "5"
        return httpx.Response(200, json={
            "users": [{"id": 11, "username": "alice"}],
            "topic_list": {"topics": [{
                "id": 22, "title": "Welcome", "slug": "welcome", "posts_count": 3,
                "reply_count": 2, "category_id": 4, "last_poster_username": "bob",
                "posters": [{"user_id": 11, "description": "Original Poster"}],
            }]},
        })

    community = _client(handler)
    assert community.categories() == [{"id": 4, "name": "General", "slug": "general", "topic_count": 2, "description": "Talk"}]
    topic = community.topics(per_page=5)[0]
    assert topic["author"] == "alice"
    assert topic["last_poster"] == "bob"
    assert topic["reply_count"] == 2
    assert topic["url"] == "/t/welcome/22"


def test_topic_details_expose_authors_and_raw_markdown_not_html():
    community = _client(lambda _request: httpx.Response(200, json={
        "id": 22, "title": "Welcome", "slug": "welcome", "category_id": 4,
        "posts_count": 2, "reply_count": 1,
        "post_stream": {"posts": [{"id": 23, "post_number": 1, "username": "alice", "raw": "Hello **community**", "cooked": "<script>unsafe</script>"}]},
    }))
    topic = community.topic(22)
    assert topic["posts"][0]["author"] == "alice"
    assert topic["posts"][0]["body"] == "Hello **community**"
    assert "<script>" not in topic["posts"][0]["body"]


def test_topic_details_fall_back_to_safe_text_when_public_api_omits_raw_markdown():
    community = _client(lambda _request: httpx.Response(200, json={
        "id": 22, "title": "Welcome", "slug": "welcome", "posts_count": 1,
        "post_stream": {"posts": [{"id": 23, "username": "alice", "cooked": "<p>Hello &amp; welcome</p><script>secret()</script>"}]},
    }))
    post = community.topic(22)["posts"][0]
    assert post["body"] == "Hello & welcome"
    assert "secret" not in post["body"]


def test_community_url_rejects_credentials_non_https_and_query_strings():
    for url in ("http://remote.test", "https://user:pass@community.test", "https://community.test/?x=1"):
        with pytest.raises(ValueError):
            DiscourseCommunityClient(url)


def test_rate_limit_is_reported_without_leaking_upstream_body():
    community = _client(lambda _request: httpx.Response(429, headers={"Retry-After": "30"}, text="private upstream detail"))
    with pytest.raises(AppError) as raised:
        community.categories()
    assert raised.value.code == "PLATFORM_RATE_LIMIT"
    assert raised.value.details == {"retry_after": "30"}
    assert "private upstream" not in str(raised.value)


def test_redirects_are_not_followed():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.test/categories.json"})

    with pytest.raises(AppError) as raised:
        _client(handler).categories()
    assert raised.value.code == "PLATFORM_UNAVAILABLE"
    assert len(requests) == 1


def test_timeout_and_server_errors_are_retryable_without_upstream_details():
    def timeout(_request):
        raise httpx.ReadTimeout("sensitive request data")

    with pytest.raises(AppError) as timeout_error:
        _client(timeout).categories()
    assert timeout_error.value.code == "PLATFORM_UNAVAILABLE"
    assert timeout_error.value.retryable is True
    assert "sensitive" not in str(timeout_error.value)

    with pytest.raises(AppError) as server_error:
        _client(lambda _request: httpx.Response(503, text="private response body")).categories()
    assert server_error.value.code == "PLATFORM_UNAVAILABLE"
    assert "private response" not in str(server_error.value)


def test_api_routes_return_categories_topics_and_topic_details(app, monkeypatch):
    community = app.state.discourse_community
    monkeypatch.setattr(community, "categories", lambda: [{"id": 4, "name": "General", "slug": "general", "topic_count": 1, "description": ""}])
    monkeypatch.setattr(community, "topics", lambda per_page: [{"id": 22, "title": "Welcome", "slug": "welcome", "category_id": 4, "reply_count": 0, "posts_count": 1, "author": "alice", "last_poster": "alice", "url": "/t/welcome/22", "excerpt": ""}])
    monkeypatch.setattr(community, "topic", lambda topic_id: {"id": topic_id, "title": "Welcome", "slug": "welcome", "category_id": 4, "reply_count": 0, "posts_count": 1, "url": "/t/welcome/22", "posts": []})
    request = Request({"type": "http", "app": app})
    from app.discourse_community_api import categories, topic, topics
    assert categories(request)["categories"][0]["name"] == "General"
    assert topics(request, 5)["topics"][0]["author"] == "alice"
    assert topic(request, 22)["url"] == "http://127.0.0.1:3000/t/welcome/22"
    path = "/api/v1/community/discourse/topics/{topic_id}"
    assert path in app.openapi()["paths"]
    topic_id_parameter = next(item for item in app.openapi()["paths"][path]["get"]["parameters"] if item["name"] == "topic_id")
    assert topic_id_parameter["schema"]["minimum"] == 1


def test_api_surfaces_safe_error_for_unavailable_community(app, monkeypatch):
    def fail():
        raise AppError("PLATFORM_UNAVAILABLE", "The Discourse community could not be reached.", True)

    monkeypatch.setattr(app.state.discourse_community, "categories", fail)
    from app.discourse_community_api import categories
    with pytest.raises(AppError) as raised:
        categories(Request({"type": "http", "app": app}))
    assert raised.value.code == "PLATFORM_UNAVAILABLE"
    from app.main import app_error_response
    assert app_error_response(raised.value).status_code == 503


def test_internal_docker_target_uses_local_host_header_and_public_browser_url():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"category_list": {"categories": []}})

    community = DiscourseCommunityClient(
        "http://discourse:3000",
        transport=httpx.MockTransport(handler),
        public_base_url="http://127.0.0.1:3000",
        host_header="localhost:3000",
    )
    assert community.categories() == []
    assert seen[0].url.host == "discourse"
    assert seen[0].headers["host"] == "localhost:3000"
    assert community.public_base_url == "http://127.0.0.1:3000"
