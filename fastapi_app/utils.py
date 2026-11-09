
import logging
import re
import time
import tomllib
from pathlib import Path
from typing import Tuple

from opentelemetry import baggage, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import \
    OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client.openmetrics.exposition import (CONTENT_TYPE_LATEST,
                                                      generate_latest)
from starlette.middleware.base import (BaseHTTPMiddleware,
                                       RequestResponseEndpoint)
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from starlette.types import ASGIApp


CORRELATION_BAGGAGE_KEYS = (
    "run_id",
    "record_id",
    "pipeline_version",
)
# Baggage arrives from untrusted clients. Keep values short and single-line so a
# crafted header cannot forge log lines or bloat span attributes.
BAGGAGE_VALUE_MAX_LENGTH = 128
UNSAFE_BAGGAGE_CHARACTERS = re.compile(r"[^A-Za-z0-9._:-]")
LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] "
    "[trace_id=%(otelTraceID)s span_id=%(otelSpanID)s "
    "resource.service.name=%(otelServiceName)s run_id=%(run_id)s "
    "record_id=%(record_id)s pipeline_version=%(pipeline_version)s "
    "app_version=%(app_version)s] - %(message)s"
)

INFO = Gauge(
    "fastapi_app_info", "FastAPI application information.", [
        "app_name"]
)
REQUESTS = Counter(
    "fastapi_requests_total", "Total count of requests by method and path.", [
        "method", "path", "app_name"]
)
RESPONSES = Counter(
    "fastapi_responses_total",
    "Total count of responses by method, path and status codes.",
    ["method", "path", "status_code", "app_name"],
)
REQUESTS_PROCESSING_TIME = Histogram(
    "fastapi_requests_duration_seconds",
    "Histogram of requests processing time by path (in seconds)",
    ["method", "path", "app_name"],
)
EXCEPTIONS = Counter(
    "fastapi_exceptions_total",
    "Total count of exceptions raised by path and exception type",
    ["method", "path", "exception_type", "app_name"],
)
REQUESTS_IN_PROGRESS = Gauge(
    "fastapi_requests_in_progress",
    "Gauge of requests by method and path currently being processed",
    ["method", "path", "app_name"],
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, app_name: str = "fastapi-app") -> None:
        super().__init__(app)
        self.app_name = app_name
        INFO.labels(app_name=self.app_name).inc()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method
        path, is_handled_path = self.get_path(request)

        if not is_handled_path:
            return await call_next(request)

        REQUESTS_IN_PROGRESS.labels(
            method=method, path=path, app_name=self.app_name).inc()
        REQUESTS.labels(method=method, path=path, app_name=self.app_name).inc()
        before_time = time.perf_counter()
        status_code = HTTP_500_INTERNAL_SERVER_ERROR
        try:
            response = await call_next(request)
        except Exception as error:
            status_code = HTTP_500_INTERNAL_SERVER_ERROR
            EXCEPTIONS.labels(method=method, path=path, exception_type=type(
                error).__name__, app_name=self.app_name).inc()
            raise
        else:
            status_code = response.status_code
        finally:
            after_time = time.perf_counter()
            # Retrieve trace ID for the latency exemplar.
            span = trace.get_current_span()
            trace_id = trace.format_trace_id(
                span.get_span_context().trace_id)

            REQUESTS_PROCESSING_TIME.labels(method=method, path=path, app_name=self.app_name).observe(
                after_time - before_time, exemplar={'TraceID': trace_id}
            )
            RESPONSES.labels(method=method, path=path,
                             status_code=status_code, app_name=self.app_name).inc()
            REQUESTS_IN_PROGRESS.labels(
                method=method, path=path, app_name=self.app_name).dec()

        return response

    @staticmethod
    def get_path(request: Request) -> Tuple[str, bool]:
        for route in request.app.routes:
            match, child_scope = route.matches(request.scope)
            if match == Match.FULL:
                return route.path, True

        return request.url.path, False


def metrics(request: Request) -> Response:
    return Response(generate_latest(REGISTRY), headers={"Content-Type": CONTENT_TYPE_LATEST})


def correlation_value(key: str) -> str | None:
    value = baggage.get_baggage(key)
    if value is None:
        return None
    return UNSAFE_BAGGAGE_CHARACTERS.sub("_", str(value)[:BAGGAGE_VALUE_MAX_LENGTH]) or None


def install_correlation_log_fields(app_version: str) -> None:
    previous_factory = logging.getLogRecordFactory()

    def record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        for key in CORRELATION_BAGGAGE_KEYS:
            setattr(record, key, correlation_value(key) or "-")
        record.app_version = app_version
        return record

    logging.setLogRecordFactory(record_factory)


def enrich_span_with_correlation(span: trace.Span, scope: dict) -> None:
    for key in CORRELATION_BAGGAGE_KEYS:
        value = correlation_value(key)
        if value:
            span.set_attribute(f"pipeline.{key}", value)


def project_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as project_file:
        return tomllib.load(project_file)["project"]["version"]


def setting_otlp(app: ASGIApp, app_name: str, app_version: str, endpoint: str,
                 log_correlation: bool = True, insecure: bool = False) -> None:
    # Setting OpenTelemetry
    # set the service name to show in traces
    resource = Resource.create(attributes={
        "service.name": app_name,
        "service.version": app_version,
    })

    # set the tracer provider
    tracer = TracerProvider(resource=resource)
    trace.set_tracer_provider(tracer)

    tracer.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=endpoint, insecure=insecure)))

    if log_correlation:
        install_correlation_log_fields(app_version)
        LoggingInstrumentor().instrument(
            set_logging_format=True,
            logging_format=LOG_FORMAT,
        )

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer,
        server_request_hook=enrich_span_with_correlation,
    )
    # Outgoing calls get CLIENT spans; Tempo pairs them with the callee's SERVER
    # spans to build the service graph (e.g. app-a -> app-b).
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer)
