import csv
import io
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.catalogue import CatalogueImporter, HttpReachabilityChecker, score_relevance
from app.database import Destination, make_database
from app.main import create_app


HEADER = "name,url,platform_key,surface,category,audience_tags,connector_key"


def row(name="AI Founders Hub", url="https://example.com/community/", platform="forum", tags="AI; founders"):
    return f'{name},{url},{platform},post,AI and founders,"{tags}",noop'


def test_valid_csv_and_relevance_are_persisted(client, app):
    response = client.post("/api/v1/destinations/import", headers={"Idempotency-Key": "catalogue-valid", "Content-Type": "text/csv"}, content=f"{HEADER}\n{row()}\n")
    assert response.status_code == 202
    job = _wait(client, response.json()["job_id"])
    report = job["result"]["catalogue_import"]
    assert report["totals"] == {"rows_seen": 1, "accepted": 1, "rejected": 0, "duplicates": 0, "warnings": 0}
    assert report["accepted"][0]["matched_audiences"] == ["AI", "Founders"]
    with app.state.session_factory() as session:
        saved = session.scalar(select(Destination))
        assert saved.url == "https://example.com/community"
        assert saved.relevance_score == 4


def test_required_headers_invalid_types_and_urls_report_row_errors(settings):
    _, factory = make_database(settings.database_url)
    importer = CatalogueImporter(factory, connector_keys={"noop"})
    missing = importer.import_csv("name,url\nA,https://example.com\n")
    assert missing["rejected"][0]["error_code"] == "REQUIRED_HEADER_MISSING"
    report = importer.import_csv(f"{HEADER},est_audience,self_promo_allowed\n{row('bad','file:///etc/passwd')},x,TRUE\n{row('bad url','http://[')},1,TRUE\n{row('bad2')},not-number,TRUE\n{row('bad3')},10,perhaps\n")
    assert [item["error_code"] for item in report["rejected"]] == ["INVALID_URL", "INVALID_URL", "INVALID_INTEGER", "INVALID_BOOLEAN"]


def test_row_limit_and_configured_file_size_are_enforced(client, settings):
    _, factory = make_database(settings.database_url)
    report = CatalogueImporter(factory, max_rows=1, connector_keys={"noop"}).import_csv(f"{HEADER}\n{row('one')}\n{row('two')}\n")
    assert report["totals"]["accepted"] == 1
    assert report["rejected"][-1]["error_code"] == "ROW_LIMIT_EXCEEDED"
    too_large = "x" * (settings.catalogue_import_max_bytes + 1)
    response = client.post("/api/v1/destinations/import", headers={"Idempotency-Key": "catalogue-large", "Content-Type": "text/csv"}, content=too_large)
    assert response.status_code == 413


def test_duplicate_normalized_url_is_reported_and_platform_is_part_of_key(settings):
    _, factory = make_database(settings.database_url)
    importer = CatalogueImporter(factory, connector_keys={"noop"})
    report = importer.import_csv(f"{HEADER}\n{row('first','HTTPS://Example.com:443/community/','forum')}\n{row('duplicate','https://example.com/community','forum')}\n{row('other platform','https://example.com/community','blog')}\n")
    assert report["totals"]["accepted"] == 2
    assert report["totals"]["duplicates"] == 1
    assert report["duplicates"][0]["retained_row"] == 2


def test_blank_rows_malformed_rows_and_mixed_results(settings):
    _, factory = make_database(settings.database_url)
    importer = CatalogueImporter(factory, connector_keys={"noop"})
    report = importer.import_csv(f"{HEADER}\n{row()}\n,,,,,,\nBad,not-url,forum,post,topic,,noop\n")
    assert report["totals"]["accepted"] == 1
    assert report["totals"]["rejected"] == 1
    assert report["warnings"][0]["warning"] == "BLANK_ROW"
    malformed = importer.import_csv(f'{HEADER}\n"unterminated')
    assert malformed["rejected"][0]["error_code"] == "MALFORMED_CSV"


def test_repeat_import_is_idempotent_and_does_not_replace_existing_settings(settings):
    _, factory = make_database(settings.database_url)
    importer = CatalogueImporter(factory, connector_keys={"noop"})
    content = f"{HEADER},self_promo_allowed\n{row()},false\n"
    first = importer.import_csv(content)
    with factory.begin() as session:
        destination = session.get(Destination, first["accepted"][0]["destination_id"])
        destination.self_promo_allowed = False
        destination.level = "L2"
    again = importer.import_csv(content)
    assert again["totals"]["accepted"] == 0
    assert again["totals"]["duplicates"] == 1
    with factory() as session:
        saved = session.scalar(select(Destination))
        assert saved.self_promo_allowed is False
        assert saved.level == "L2"


def test_scoring_is_conservative_and_explainable():
    assert score_relevance({})["relevance_score"] == 1
    assert score_relevance({})["warnings"]
    one = score_relevance({"name": "AI research forum"})
    assert one["relevance_score"] == 3
    assert one["matched_audiences"] == ["AI"]
    five = score_relevance({"name": "AI video marketing SaaS creator startup", "audience_tags": ["AI", "video", "marketing", "SaaS", "creators", "founders"]})
    assert five["relevance_score"] == 5
    assert len(five["matched_audiences"]) == 6


def test_optional_reachability_adapter_and_ssrf_blocking(settings):
    observed = []
    _, factory = make_database(settings.database_url)
    importer = CatalogueImporter(factory, connector_keys={"noop"}, reachability_checker=lambda url: observed.append(url) or {"status": "reachable"})
    report = importer.import_csv(f"{HEADER}\n{row()}\n")
    assert observed == ["https://example.com/community"]
    assert report["accepted"][0]["reachability"]["status"] == "reachable"
    checker = HttpReachabilityChecker()
    assert checker("http://127.0.0.1/hook")["status"] == "blocked"


@pytest.mark.parametrize("check", [lambda _url: {"status": "unreachable", "reason": "timeout"}, lambda _url: {"status": "blocked", "reason": "redirect"}, lambda _url: (_ for _ in ()).throw(TimeoutError())])
def test_reachability_failure_is_a_warning_not_a_rejection(settings, check):
    _, factory = make_database(settings.database_url)
    importer = CatalogueImporter(factory, connector_keys={"noop"}, reachability_checker=check)
    report = importer.import_csv(f"{HEADER}\n{row()}\n")
    assert report["totals"]["accepted"] == 1
    assert report["totals"]["rejected"] == 0
    assert report["accepted"][0]["warnings"]


def test_import_api_idempotency_and_rules_pending_until_explicit_approval(client, app):
    csv_content = f"{HEADER}\n{row()}\n"
    first = client.post("/api/v1/destinations/import", headers={"Idempotency-Key": "catalogue-idem", "Content-Type": "text/csv"}, content=csv_content)
    second = client.post("/api/v1/destinations/import", headers={"Idempotency-Key": "catalogue-idem", "Content-Type": "text/csv"}, content=csv_content)
    assert first.json()["job_id"] == second.json()["job_id"]
    different = client.post("/api/v1/destinations/import", headers={"Idempotency-Key": "catalogue-idem", "Content-Type": "text/csv"}, content=csv_content + "\n")
    assert different.status_code == 409
    job = _wait(client, first.json()["job_id"])
    destination_id = job["result"]["catalogue_import"]["accepted"][0]["destination_id"]
    proposed = client.post(f"/api/v1/destinations/{destination_id}/rule-proposals", headers={"Idempotency-Key": "rule-proposal-1"}, json={"source_url": "https://example.com/rules", "source_excerpt": "Self promotion is not permitted.", "proposed_rules": {"self_promo_allowed": False}, "confidence": 0.7, "warnings": ["Manual review required."]})
    proposal = proposed.json()
    assert proposal["status"] == "pending"
    with app.state.session_factory() as session:
        assert session.get(Destination, destination_id).self_promo_allowed is True
    approved = client.post(f"/api/v1/destinations/rule-proposals/{proposal['id']}/approve", headers={"Idempotency-Key": "rule-approval-1"}, json={"confirm": True})
    assert approved.json()["status"] == "approved"
    with app.state.session_factory() as session:
        assert session.get(Destination, destination_id).self_promo_allowed is False


def test_1000_row_import_benchmark(settings, capsys):
    _, factory = make_database(settings.database_url)
    importer = CatalogueImporter(factory, max_rows=5000, connector_keys={"noop"})
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER.split(","))
    for index in range(1000):
        writer.writerow([f"AI Founder Forum {index}", f"https://catalogue.example/community/{index}", "forum", "post", "AI", "AI; founders", "noop"])
    started = time.perf_counter()
    result = importer.import_csv(buffer.getvalue())
    elapsed = time.perf_counter() - started
    print({"elapsed_seconds": round(elapsed, 3), **result["totals"]})
    assert result["totals"]["accepted"] == 1000
    assert elapsed < 300


def _wait(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        job = response.json()
        if job["status"] in {"succeeded", "partial", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("catalogue import job did not finish")
