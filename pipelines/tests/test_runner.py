import csv
from pathlib import Path

import pytest
from opentelemetry import baggage
from opentelemetry.trace import StatusCode

from pipelines.runner import (
    PipelineDefinition,
    read_rows,
    record_context,
    resolve_target,
    run_pipeline,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        text: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "request failed", request=None, response=None
            )


class FakeClient:
    def __init__(self, responses: list[FakeResponse], **_: object) -> None:
        self.responses = iter(responses)

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, _: str, headers: dict[str, str] | None = None) -> FakeResponse:
        if headers is not None:
            assert "baggage" in headers
        return next(self.responses)


def write_input(path: Path) -> None:
    path.write_text("record_id,name\nrecord-1,Ada\nrecord-2,Grace\n", encoding="utf-8")


def test_pipeline_writes_auditable_rows(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    write_input(input_path)
    monkeypatch.setattr(
        "pipelines.runner.httpx.Client",
        lambda **kwargs: FakeClient(
            [
                FakeResponse(200, "", {"X-App-Version": "4.5.6"}),
                FakeResponse(200, "first"),
                FakeResponse(200, "second"),
            ],
            **kwargs,
        ),
    )

    run_id, failures = run_pipeline(
        PipelineDefinition("enrich", "/io_task"),
        input_path,
        output_path,
        "http://app-a:8000",
        pipeline_version="1.2.3",
        run_id="run-123",
    )

    assert run_id == "run-123"
    assert failures == 0
    with output_path.open(newline="", encoding="utf-8") as output_file:
        rows = list(csv.DictReader(output_file))
    assert [
        (
            row["run_id"],
            row["record_id"],
            row["pipeline_version"],
            row["target"],
            row["target_url"],
            row["app_version"],
            row["outcome"],
        )
        for row in rows
    ] == [
        ("run-123", "record-1", "1.2.3", "direct", "http://app-a:8000", "4.5.6", "succeeded"),
        ("run-123", "record-2", "1.2.3", "direct", "http://app-a:8000", "4.5.6", "succeeded"),
    ]
    assert not output_path.with_suffix(".csv.tmp").exists()


def test_pipeline_records_failure_after_retries(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    write_input(input_path)
    monkeypatch.setattr(
        "pipelines.runner.time.sleep", lambda _: None,
    )
    monkeypatch.setattr(
        "pipelines.runner.httpx.Client",
        lambda **kwargs: FakeClient(
            [FakeResponse(200, "", {"X-App-Version": "0.1.0"})]
            + [FakeResponse(500, "error")] * 6,
            **kwargs,
        ),
    )

    _, failures = run_pipeline(
        PipelineDefinition("enrich", "/io_task"),
        input_path,
        output_path,
        "http://app-a:8000",
        run_id="run-456",
    )

    assert failures == 2
    with output_path.open(newline="", encoding="utf-8") as output_file:
        rows = list(csv.DictReader(output_file))
    assert all(row["outcome"] == "failed" for row in rows)
    assert all(row["attempts"] == "3" for row in rows)


def test_record_context_exposes_run_and_record_ids() -> None:
    with record_context("run-789", "record-3", "1.2.3", "4.5.6"):
        assert baggage.get_baggage("run_id") == "run-789"
        assert baggage.get_baggage("record_id") == "record-3"
        assert baggage.get_baggage("pipeline_version") == "1.2.3"
        assert baggage.get_baggage("app_version") == "4.5.6"

    assert baggage.get_baggage("run_id") is None
    assert baggage.get_baggage("record_id") is None
    assert baggage.get_baggage("pipeline_version") is None
    assert baggage.get_baggage("app_version") is None


def test_pipeline_refuses_to_overwrite_a_completed_output(tmp_path: Path, spans) -> None:
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    write_input(input_path)
    output_path.write_text("existing output", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refusing to overwrite existing output"):
        run_pipeline(
            PipelineDefinition("enrich", "/io_task"),
            input_path,
            output_path,
            "http://app-a:8000",
            run_id="run-999",
        )

    assert finished_span(spans, "pipeline.run").status.status_code is StatusCode.ERROR
    assert output_path.read_text(encoding="utf-8") == "existing output"


def test_pipeline_refuses_to_overwrite_an_interrupted_output(tmp_path: Path) -> None:
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    write_input(input_path)
    output_path.with_suffix(".csv.tmp").write_text("incomplete output", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refusing to overwrite interrupted output"):
        run_pipeline(
            PipelineDefinition("enrich", "/io_task"),
            input_path,
            output_path,
            "http://app-a:8000",
            run_id="run-1000",
        )


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("name\nAda\n", "record_id column"),
        ("record_id,name\n1,Ada\n1,Grace\n", "unique per input file"),
    ],
)
def test_read_rows_rejects_invalid_record_ids(tmp_path: Path, contents: str, message: str) -> None:
    input_path = tmp_path / "input.csv"
    input_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        read_rows(input_path)


def test_resolve_target_resolves_latest_to_a_concrete_target(tmp_path: Path) -> None:
    targets_path = tmp_path / "targets.toml"
    targets_path.write_text(
        "[targets.app-v2]\nurl = \"http://app-a-v2:8000\"\nexpected_version = \"1.1.0\"\n\n[aliases]\nlatest = \"app-v2\"\n",
        encoding="utf-8",
    )

    target = resolve_target(targets_path, "latest")

    assert target.name == "app-v2"
    assert target.url == "http://app-a-v2:8000"
    assert target.expected_version == "1.1.0"


def finished_span(spans, name: str):
    matches = [span for span in spans.get_finished_spans() if span.name == name]
    assert len(matches) == 1, [span.name for span in spans.get_finished_spans()]
    return matches[0]


def test_client_errors_are_not_retried(tmp_path: Path, monkeypatch, spans) -> None:
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    write_input(input_path)
    monkeypatch.setattr("pipelines.runner.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "pipelines.runner.httpx.Client",
        lambda **kwargs: FakeClient(
            [FakeResponse(200, "", {"X-App-Version": "0.1.0"})]
            + [FakeResponse(404, "missing")] * 2,
            **kwargs,
        ),
    )

    _, failures = run_pipeline(
        PipelineDefinition("enrich", "/io_task"), input_path, output_path, "http://app-a:8000"
    )

    assert failures == 2
    with output_path.open(newline="", encoding="utf-8") as output_file:
        rows = list(csv.DictReader(output_file))
    assert [(row["attempts"], row["status_code"]) for row in rows] == [("1", "404"), ("1", "404")]
    record_spans = [s for s in spans.get_finished_spans() if s.name == "pipeline.record"]
    assert [event.attributes["pipeline.retryable"] for s in record_spans for event in s.events] == [
        False,
        False,
    ]


def test_run_span_reports_failed_records(tmp_path: Path, monkeypatch, spans) -> None:
    input_path = tmp_path / "input.csv"
    write_input(input_path)
    monkeypatch.setattr("pipelines.runner.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "pipelines.runner.httpx.Client",
        lambda **kwargs: FakeClient(
            [
                FakeResponse(200, "", {"X-App-Version": "0.1.0"}),
                FakeResponse(200, "ok"),
                FakeResponse(400, "bad request"),
            ],
            **kwargs,
        ),
    )

    run_pipeline(
        PipelineDefinition("enrich", "/io_task"),
        input_path,
        tmp_path / "output.csv",
        "http://app-a:8000",
        run_id="run-spans",
    )

    run_span = finished_span(spans, "pipeline.run")
    assert run_span.status.status_code is StatusCode.ERROR
    assert run_span.attributes["pipeline.records"] == 2
    assert run_span.attributes["pipeline.failed_records"] == 1
    assert run_span.attributes["pipeline.app_version"] == "0.1.0"


def test_target_version_mismatch_is_traced_and_writes_no_output(
    tmp_path: Path, monkeypatch, spans
) -> None:
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    write_input(input_path)
    monkeypatch.setattr(
        "pipelines.runner.httpx.Client",
        lambda **kwargs: FakeClient([FakeResponse(200, "", {"X-App-Version": "9.9.9"})], **kwargs),
    )

    with pytest.raises(RuntimeError, match="did not match expected 0.1.0"):
        run_pipeline(
            PipelineDefinition("enrich", "/io_task"),
            input_path,
            output_path,
            "http://app-a:8000",
            expected_app_version="0.1.0",
        )

    run_span = finished_span(spans, "pipeline.run")
    assert run_span.status.status_code is StatusCode.ERROR
    assert run_span.events[0].name == "exception"
    assert not output_path.exists()
    assert not output_path.with_suffix(".csv.tmp").exists()
