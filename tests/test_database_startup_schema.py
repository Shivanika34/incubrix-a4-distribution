from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.artifacts import EphemeralArtifactStore
from app.database import Destination, make_database
from app.main import create_app


def test_fresh_sqlite_database_initializes_destinations_table(settings):
    engine, session_factory = make_database(settings.database_url)
    try:
        assert inspect(engine).has_table("destinations")
        with session_factory() as session:
            assert session.query(Destination).count() == 0
    finally:
        engine.dispose()


def test_app_startup_restores_missing_table_before_destination_requests(
    settings, tmp_path
):
    app = create_app(
        settings=settings,
        artifact_store=EphemeralArtifactStore(str(tmp_path / "artifacts"), ""),
    )
    setup_engine, _ = make_database(settings.database_url)
    try:
        with setup_engine.begin() as connection:
            connection.execute(text("DROP TABLE destinations"))
    finally:
        setup_engine.dispose()

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        empty = client.get("/api/v1/destinations")
        assert empty.status_code == 200
        assert empty.json() == []

        created = client.post(
            "/api/v1/destinations",
            headers={"Idempotency-Key": "startup-schema-destination"},
            json={
                "name": "Startup schema destination",
                "url": "https://dev.to",
                "platform_key": "devto",
                "surface": "article",
                "connector_key": "devto",
            },
        )
        assert created.status_code == 201
        listed = client.get("/api/v1/destinations")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [created.json()["id"]]
