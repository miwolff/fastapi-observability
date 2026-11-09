# Scheduled CSV Pipelines

This image provides one-shot CSV pipeline commands for the Compose observability demo.

- `pipelines enrich` calls `/io_task` for every input row.
- `pipelines score` calls `/chain` for every input row, fanning out to `app-b` and `app-c`.

Each execution has a `run_id`, and each input row has a `record_id`. Both identifiers are included in CSV output, logs, OpenTelemetry span attributes, and W3C baggage sent to FastAPI.

Telemetry:

- Traces: OTLP/gRPC (`--otlp-endpoint`, `OTLP_GRPC_ENDPOINT`).
- Logs: OTLP/HTTP to Loki when `--otlp-logs-endpoint` / `OTLP_LOGS_ENDPOINT` is set; always also written to stderr at `--console-log-level`.
- Metrics: none emitted directly. Tempo derives span metrics from the traces.

Exit codes: `0` success, `1` at least one record failed, `2` the run aborted.

## Run locally

Start the Compose stack from the repository root first (`app-a` listens on `localhost:8000`), then:

```bash
uv sync
printf '[targets.local]\nurl = "http://localhost:8000"\nexpected_version = "0.1.0"\n\n[aliases]\nlatest = "local"\n' > /tmp/targets.toml

uv run pipelines enrich --input data/input/enrich.csv --output /tmp/enrich.csv \
  --targets-file /tmp/targets.toml --otlp-endpoint localhost:4317 --otlp-insecure

# Or derive a run-scoped filename under an output directory.
uv run pipelines score --input data/input/score.csv --output-dir /tmp/pipeline-output \
  --targets-file /tmp/targets.toml
```

Tempo has no published host port, so a local run cannot export traces unless you publish port 4317. The run itself still succeeds; the exporter logs connection errors and retries for a few seconds before the process exits.

## Tests

```bash
uv run --frozen pytest -q
```
