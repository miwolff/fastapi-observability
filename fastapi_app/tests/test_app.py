import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry import baggage, context

import main
from utils import BAGGAGE_VALUE_MAX_LENGTH, correlation_value


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def test_responses_report_the_package_version(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["X-App-Version"] == main.APP_VERSION == "0.1.0"


def test_metrics_use_route_templates_and_carry_trace_exemplars(client: TestClient) -> None:
    client.get("/items/42")

    body = client.get("/metrics").text

    # The route template, not the concrete URL, keeps label cardinality bounded.
    assert 'fastapi_requests_total{app_name="app",method="GET",path="/items/{item_id}"}' in body
    assert "/items/42" not in body
    assert '# {TraceID="' in body


def test_unknown_paths_are_not_recorded_as_metric_labels(client: TestClient) -> None:
    client.get("/does-not-exist/12345")

    assert "/does-not-exist" not in client.get("/metrics").text


def test_baggage_values_cannot_forge_log_lines() -> None:
    hostile = "run-1\n2026-01-01 00:00:00,000 INFO forged" + "x" * 500
    token = context.attach(baggage.set_baggage("run_id", hostile))
    try:
        value = correlation_value("run_id")
        record = logging.getLogger("test").makeRecord(
            "test", logging.INFO, __file__, 1, "message", (), None
        )
    finally:
        context.detach(token)

    assert "\n" not in value and " " not in value
    assert len(value) == BAGGAGE_VALUE_MAX_LENGTH
    assert record.run_id == value


def test_missing_baggage_is_logged_as_placeholder() -> None:
    record = logging.getLogger("test").makeRecord(
        "test", logging.INFO, __file__, 1, "message", (), None
    )

    assert (record.run_id, record.record_id, record.pipeline_version) == ("-", "-", "-")
    assert record.app_version == "0.1.0"
