import argparse
import logging
import os
import re
from pathlib import Path
from uuid import uuid4

from pipelines.runner import (
    PipelineDefinition,
    project_version,
    record_context,
    resolve_target,
    run_pipeline,
)
from pipelines.telemetry import configure_telemetry

PIPELINES = {
    "enrich": PipelineDefinition(name="enrich", endpoint="/io_task"),
    "score": PipelineDefinition(name="score", endpoint="/chain"),
}

# The run ID becomes a file name under --output-dir, so restrict it to a safe slug.
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def run_id_argument(value: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "run ID must be 1-128 characters of letters, digits, '.', '_' or '-'"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument("--input", type=Path, required=True, help="CSV input path")
    output_group = options.add_mutually_exclusive_group(required=True)
    output_group.add_argument("--output", type=Path, help="CSV output path")
    output_group.add_argument(
        "--output-dir", type=Path, help="Directory where a run-scoped CSV output file is written"
    )
    options.add_argument(
        "--run-id", type=run_id_argument, help="Execution identifier; generated when omitted"
    )
    options.add_argument(
        "--target",
        default=os.environ.get("FASTAPI_TARGET", "latest"),
        help="Named target from the targets manifest",
    )
    options.add_argument(
        "--targets-file",
        type=Path,
        default=Path(os.environ.get("PIPELINE_TARGETS_FILE", "/app/config/targets.toml")),
        help="TOML deployment target manifest",
    )
    options.add_argument(
        "--otlp-endpoint",
        default=os.environ.get("OTLP_GRPC_ENDPOINT", "tempo:4317"),
        help="OTLP gRPC endpoint for traces",
    )
    options.add_argument(
        "--otlp-insecure",
        action="store_true",
        default=os.environ.get("OTLP_INSECURE", "false").lower() == "true",
        help="Use an insecure OTLP gRPC connection",
    )
    options.add_argument(
        "--otlp-logs-endpoint",
        default=os.environ.get("OTLP_LOGS_ENDPOINT") or None,
        help="OTLP/HTTP logs endpoint, e.g. http://loki:3100/otlp/v1/logs; "
        "console logging only when omitted",
    )
    options.add_argument(
        "--console-log-level",
        default=os.environ.get("PIPELINE_CONSOLE_LOG_LEVEL", "INFO").upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Minimum level written to stderr",
    )

    parser = argparse.ArgumentParser(prog="pipelines", description="Run a CSV pipeline")
    commands = parser.add_subparsers(dest="pipeline", required=True)
    for name, definition in PIPELINES.items():
        commands.add_parser(
            name, parents=[options], help=f"call {definition.endpoint} for every row"
        )
    return parser


def main(argv: list[str] | None = None) -> None:
    arguments = build_parser().parse_args(argv)
    pipeline_name = arguments.pipeline
    run_id = arguments.run_id or str(uuid4())
    output_path = arguments.output or arguments.output_dir / pipeline_name / f"{run_id}.csv"
    pipeline_version = project_version()

    # Telemetry comes first so configuration errors below are exported too.
    configure_telemetry(
        service_name=f"pipelines-{pipeline_name}",
        service_version=pipeline_version,
        traces_endpoint=arguments.otlp_endpoint,
        insecure=arguments.otlp_insecure,
        logs_endpoint=arguments.otlp_logs_endpoint,
        console_level=getattr(logging, arguments.console_log_level),
    )
    try:
        target = resolve_target(arguments.targets_file, arguments.target)
        run_id, failures = run_pipeline(
            definition=PIPELINES[pipeline_name],
            input_path=arguments.input,
            output_path=output_path,
            base_url=target.url,
            target_name=target.name,
            expected_app_version=target.expected_version,
            pipeline_version=pipeline_version,
            run_id=run_id,
        )
    except Exception:
        # The run span has already recorded the exception; this log keeps the
        # run ID so the failure is also searchable in Loki.
        with record_context(run_id, "-", pipeline_version, "-"):
            logging.exception("Pipeline %s run %s aborted", pipeline_name, run_id)
        raise SystemExit(2) from None

    print(f"pipeline={pipeline_name} run_id={run_id} failures={failures}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
