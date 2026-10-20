import json
from datetime import datetime, timezone
from pathlib import Path

from app.rules import check_rules


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_table_driven_posting_rules():
    cases = json.loads((Path(__file__).parent / "data" / "rule_cases.json").read_text())
    assert len(cases) >= 15
    for case in cases:
        result = check_rules(case["rendition"], case["destination"], case["history"], now=NOW)
        assert result.outcome == case["outcome"], case["name"]
        if "code" in case:
            assert result.error_code == case["code"], case["name"]
        if "body" in case:
            assert result.rendition["body"] == case["body"], case["name"]


def test_rules_run_before_connector_publish_and_are_recorded(client, app):
    response = client.post(
        "/connector/v1/noop/publish",
        headers={"Idempotency-Key": "rule-before-publish"},
        json={"rendition": {"is_promotional": True}, "destination": {"self_promo_allowed": False}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DEST_SPEC_VIOLATION"
    from app.database import RuleDecision
    with app.state.session_factory() as session:
        records = session.query(RuleDecision).all()
    assert len(records) == 1
    assert records[0].outcome == "blocked"
