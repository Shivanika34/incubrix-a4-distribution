import json
from app.errors import AppError
from app.main import app_error_response


def test_error_response_uses_shared_shape(client):
    response = app_error_response(AppError("SAMPLE_ERROR", "A sample error", False))
    error = json.loads(response.body)["error"]
    assert set(error) == {"code", "message", "retryable", "details"}
    assert error["code"] == "SAMPLE_ERROR"
    assert error["retryable"] is False


def test_request_correlation_id_is_returned_and_logged(client, caplog):
    response = client.get("/health", headers={"X-Correlation-Id": "trace-test-42"})
    assert response.headers["X-Correlation-Id"] == "trace-test-42"
    event = next(record for record in caplog.records if record.getMessage() == "request completed")
    assert event.correlation_id == "trace-test-42"


def test_missing_correlation_id_is_generated(client):
    response = client.get("/health")
    assert response.headers.get("X-Correlation-Id")
