import logging

from opentelemetry import trace

from pipelines.runner import record_context
from pipelines.telemetry import CONSOLE_FORMAT, CorrelationFormatter, install_correlation_fields


def test_log_lines_carry_trace_and_baggage_ids_without_mutating_the_record() -> None:
    install_correlation_fields()
    formatter = CorrelationFormatter(CONSOLE_FORMAT)
    tracer = trace.get_tracer("test")

    with record_context("run-1", "record-7", "1.0.0", "2.0.0"):
        with tracer.start_as_current_span("work") as span:
            record = logging.getLogger("test").makeRecord(
                "test", logging.INFO, __file__, 1, "hello", (), None
            )
            line = formatter.format(record)
            trace_id = trace.format_trace_id(span.get_span_context().trace_id)

    assert f"trace_id={trace_id}" in line
    assert "run_id=run-1 record_id=record-7 pipeline_version=1.0.0 app_version=2.0.0" in line
    assert line.endswith("hello")
    # Only the baggage fields become OTLP log attributes; trace IDs are native fields.
    assert record.run_id == "run-1"
    assert not hasattr(record, "trace_id")
