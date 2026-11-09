import csv
import logging
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import httpx
from opentelemetry import baggage, context, trace
from opentelemetry.propagate import inject


@dataclass(frozen=True)
class PipelineDefinition:
    name: str
    endpoint: str


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    expected_version: str


@dataclass(frozen=True)
class RunContext:
    """Identifies one execution; copied onto every span, log and output row."""

    definition: PipelineDefinition
    run_id: str
    pipeline_version: str
    target_name: str
    base_url: str
    app_version: str = "-"

    def span_attributes(self) -> dict[str, str]:
        return {
            "pipeline.name": self.definition.name,
            "pipeline.run_id": self.run_id,
            "pipeline.version": self.pipeline_version,
            "pipeline.target": self.target_name,
            "pipeline.target_url": self.base_url,
            "pipeline.app_version": self.app_version,
        }


OUTPUT_FIELDS = (
    "run_id",
    "record_id",
    "pipeline",
    "pipeline_version",
    "target",
    "target_url",
    "app_version",
    "started_at",
    "finished_at",
    "endpoint",
    "status_code",
    "attempts",
    "outcome",
    "result",
    "error",
)


def project_version() -> str:
    return version("scheduled-pipelines")


def resolve_target(targets_path: Path, target_name: str) -> Target:
    with targets_path.open("rb") as targets_file:
        configuration = tomllib.load(targets_file)

    resolved_name = configuration.get("aliases", {}).get(target_name, target_name)
    target = configuration.get("targets", {}).get(resolved_name)
    if not target:
        raise ValueError(f"Unknown pipeline target: {target_name}")
    if not target.get("url") or not target.get("expected_version"):
        raise ValueError(f"Target {resolved_name} requires url and expected_version")
    return Target(resolved_name, target["url"], target["expected_version"])


@contextmanager
def record_context(run_id: str, record_id: str, pipeline_version: str, app_version: str):
    """Attach correlation IDs as W3C baggage, propagated to FastAPI with each request."""
    values = {
        "run_id": run_id,
        "record_id": record_id,
        "pipeline_version": pipeline_version,
        "app_version": app_version,
    }
    ctx = context.get_current()
    for key, value in values.items():
        ctx = baggage.set_baggage(key, value, context=ctx)
    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


def run_pipeline(
    definition: PipelineDefinition,
    input_path: Path,
    output_path: Path,
    base_url: str,
    target_name: str = "direct",
    expected_app_version: str | None = None,
    pipeline_version: str | None = None,
    run_id: str | None = None,
    timeout_seconds: float = 5.0,
    max_attempts: int = 3,
) -> tuple[str, int]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    run = RunContext(
        definition=definition,
        run_id=run_id or str(uuid4()),
        pipeline_version=pipeline_version or project_version(),
        target_name=target_name,
        base_url=base_url.rstrip("/"),
    )
    tracer = trace.get_tracer("pipelines.runner")
    failures = 0

    # The run span opens before any validation, so every aborted run (bad input,
    # existing output, unreachable or mis-versioned target) is an ERROR trace.
    with (
        record_context(run.run_id, "-", run.pipeline_version, "-"),
        tracer.start_as_current_span("pipeline.run") as run_span,
    ):
        run_span.set_attributes(run.span_attributes())
        rows = read_rows(input_path)
        run_span.set_attribute("pipeline.records", len(rows))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {output_path}")
        temporary_output_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
        if temporary_output_path.exists():
            raise FileExistsError(
                f"Refusing to overwrite interrupted output: {temporary_output_path}"
            )

        with httpx.Client(timeout=httpx.Timeout(timeout_seconds)) as client:
            run = replace(
                run, app_version=fetch_app_version(client, run.base_url, expected_app_version)
            )
            run_span.set_attribute("pipeline.app_version", run.app_version)

            with record_context(run.run_id, "-", run.pipeline_version, run.app_version):
                logging.info("Starting %s with %s records", definition.name, len(rows))
                with temporary_output_path.open("x", newline="", encoding="utf-8") as output_file:
                    writer = csv.DictWriter(output_file, fieldnames=OUTPUT_FIELDS)
                    writer.writeheader()
                    for row in rows:
                        result = process_row(client, tracer, run, row, max_attempts)
                        writer.writerow(result)
                        output_file.flush()
                        failures += result["outcome"] == "failed"

                temporary_output_path.replace(output_path)
                run_span.set_attribute("pipeline.failed_records", failures)
                if failures:
                    run_span.set_status(
                        trace.Status(trace.StatusCode.ERROR, f"{failures} records failed")
                    )
                log = logging.warning if failures else logging.info
                log(
                    "Finished %s run with %s failed records; output=%s",
                    definition.name,
                    failures,
                    output_path,
                )
    return run.run_id, failures


def is_retryable(status_code: int) -> bool:
    """Server errors and throttling may succeed later; other client errors will not."""
    return status_code >= 500 or status_code in (408, 429)


def fetch_app_version(
    client: httpx.Client,
    base_url: str,
    expected_version: str | None = None,
) -> str:
    headers: dict[str, str] = {}
    inject(headers)
    response = client.get(f"{base_url}/health", headers=headers)
    response.raise_for_status()
    app_version = response.headers.get("X-App-Version")
    if not app_version:
        raise RuntimeError("Target FastAPI service did not provide X-App-Version")
    if expected_version and app_version != expected_version:
        raise RuntimeError(
            f"Target FastAPI version {app_version} did not match expected {expected_version}"
        )
    return app_version


def read_rows(input_path: Path) -> list[dict[str, str]]:
    with input_path.open(newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        if not reader.fieldnames or "record_id" not in reader.fieldnames:
            raise ValueError("Input CSV must include a record_id column")
        rows = list(reader)

    record_ids = [row["record_id"] for row in rows]
    if not all(record_ids) or len(record_ids) != len(set(record_ids)):
        raise ValueError("record_id values must be non-empty and unique per input file")
    return rows


def process_row(
    client: httpx.Client,
    tracer: trace.Tracer,
    run: RunContext,
    row: dict[str, str],
    max_attempts: int,
) -> dict[str, str | int]:
    record_id = row["record_id"]
    endpoint = run.definition.endpoint
    url = f"{run.base_url}{endpoint}"
    started_at = utc_now()

    with (
        record_context(run.run_id, record_id, run.pipeline_version, run.app_version),
        tracer.start_as_current_span("pipeline.record") as span,
    ):
        span.set_attributes({**run.span_attributes(), "pipeline.record_id": record_id})
        for attempt in range(1, max_attempts + 1):
            headers: dict[str, str] = {}
            inject(headers)
            status_code: int | str = ""
            body = error = ""
            try:
                response = client.get(url, headers=headers)
                status_code = response.status_code
                response.raise_for_status()
                body = response.text
                break
            except httpx.HTTPError as exception:
                error = str(exception)
                retryable = not isinstance(exception, httpx.HTTPStatusError) or is_retryable(
                    status_code
                )
                span.add_event(
                    "pipeline.attempt_failed",
                    {
                        "pipeline.attempt": attempt,
                        "pipeline.retryable": retryable,
                        "error.type": type(exception).__name__,
                    },
                )
                logging.warning(
                    "Pipeline request failed for %s attempt %s/%s: %s",
                    endpoint,
                    attempt,
                    max_attempts,
                    exception,
                )
                if not retryable or attempt == max_attempts:
                    break
                time.sleep(min(0.25 * 2 ** (attempt - 1), 1.0))

        span.set_attribute("pipeline.attempts", attempt)
        if status_code:
            span.set_attribute("http.response.status_code", status_code)
        if error:
            span.set_status(trace.Status(trace.StatusCode.ERROR, error))
            logging.error("Record %s failed after %s attempts: %s", record_id, attempt, error)
        else:
            logging.info("Processed %s via %s", record_id, endpoint)

    return {
        "run_id": run.run_id,
        "record_id": record_id,
        "pipeline": run.definition.name,
        "pipeline_version": run.pipeline_version,
        "target": run.target_name,
        "target_url": run.base_url,
        "app_version": run.app_version,
        "started_at": started_at,
        "finished_at": utc_now(),
        "endpoint": url,
        "status_code": status_code,
        "attempts": attempt,
        "outcome": "failed" if error else "succeeded",
        "result": body,
        "error": error,
    }


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
