"""OpenTelemetry setup for one-shot pipeline processes.

Traces go to Tempo over OTLP/gRPC. Logs go to Loki's native OTLP/HTTP endpoint,
because scheduled job containers are created by Ofelia and therefore do not use
the Compose Loki logging driver. Console logs remain for `docker compose run`.
"""

import logging

from opentelemetry import baggage, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

CORRELATION_KEYS = ("run_id", "record_id", "pipeline_version", "app_version")

# The bracketed prefix matches the FastAPI log format, so the same Loki
# derived-field regex (trace_id=...) and line filters work for every service.
CORRELATION_FORMAT = (
    "[trace_id=%(trace_id)s span_id=%(span_id)s run_id=%(run_id)s "
    "record_id=%(record_id)s pipeline_version=%(pipeline_version)s "
    "app_version=%(app_version)s] %(message)s"
)
CONSOLE_FORMAT = "%(asctime)s %(levelname)s [%(name)s] " + CORRELATION_FORMAT

class CorrelationFormatter(logging.Formatter):
    """Adds trace and baggage identifiers at format time.

    Values are read when the record is formatted rather than stored on the
    record, so the OTLP handler does not duplicate them as log attributes;
    Loki already receives trace and span IDs as native OTLP fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Format a copy: handlers share one record, and the OTLP handler turns
        # every extra record attribute into a log attribute.
        record = logging.makeLogRecord(record.__dict__)
        span_context = trace.get_current_span().get_span_context()
        record.trace_id = (
            trace.format_trace_id(span_context.trace_id) if span_context.is_valid else "0"
        )
        record.span_id = (
            trace.format_span_id(span_context.span_id) if span_context.is_valid else "0"
        )
        return super().format(record)


def install_correlation_fields() -> None:
    """Copy correlation baggage onto every log record (and thus OTLP attributes)."""
    previous_factory = logging.getLogRecordFactory()

    def record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        values = baggage.get_all()
        for key in CORRELATION_KEYS:
            setattr(record, key, str(values.get(key, "-")))
        return record

    logging.setLogRecordFactory(record_factory)


def configure_telemetry(
    service_name: str,
    service_version: str,
    traces_endpoint: str,
    insecure: bool,
    logs_endpoint: str | None = None,
    console_level: int = logging.INFO,
) -> None:
    resource = Resource.create(
        {"service.name": service_name, "service.version": service_version}
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint, insecure=insecure))
    )
    trace.set_tracer_provider(tracer_provider)
    # CLIENT spans for each request let Tempo draw pipelines-* -> app-a in the
    # service graph. Context is still injected explicitly in the runner, so
    # propagation does not depend on this instrumentation.
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)

    install_correlation_fields()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(CorrelationFormatter(CONSOLE_FORMAT))
    handlers: list[logging.Handler] = [console_handler]

    if logs_endpoint:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=logs_endpoint))
        )
        set_logger_provider(logger_provider)
        otlp_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        otlp_handler.setFormatter(CorrelationFormatter(CORRELATION_FORMAT))
        handlers.append(otlp_handler)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    # The OTLP exporters log through urllib3/grpc; keep their chatter out of Loki.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
