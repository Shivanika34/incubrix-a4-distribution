import base64
import hashlib
import hmac
import json

import pytest

from app.connectors.ghost import GhostConnector
from app.errors import AppError


def decode_part(token: str, index: int) -> dict:
    value = token.split(".")[index]
    value += "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value))


def test_ghost_admin_jwt_has_required_claims_and_header():
    key = "integration-id:" + "ab" * 32
    token = GhostConnector._admin_jwt(key, now=1_700_000_000)
    header = decode_part(token, 0)
    payload = decode_part(token, 1)

    assert header == {"alg": "HS256", "typ": "JWT", "kid": "integration-id"}
    assert payload == {"iat": 1_700_000_000, "exp": 1_700_000_300, "aud": "/admin/"}
    assert len(token.split(".")) == 3
    signing_input = ".".join(token.split(".")[:2]).encode("ascii")
    expected = hmac.new(bytes.fromhex("ab" * 32), signing_input, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(expected).rstrip(b"=").decode("ascii")
    assert token.split(".")[2] == encoded


def test_ghost_admin_jwt_rejects_malformed_key():
    with pytest.raises(AppError) as error:
        GhostConnector._admin_jwt("not-a-ghost-key", now=1_700_000_000)
    assert error.value.code == "CONNECTION_AUTH_EXPIRED"
