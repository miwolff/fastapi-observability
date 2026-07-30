# FastAPI Observability Demo Application

This a demo application built with FastAPI to showcase observability features including:

- Tracing: using [OpenTelemetry Python SDK](https://github.com/open-telemetry/opentelemetry-python)
- Metrics: using [Prometheus Python Client](https://github.com/prometheus/client_python)
- Logging: using Python's logging module and [OpenTelemetry logging integration](https://pypi.org/project/opentelemetry-instrumentation-logging/)

## Development

The project requires Python 3.12 or newer. `uv` installs a managed interpreter when needed and uses `uv.lock` to reproduce the resolved dependency set.

```bash
uv python install 3.12
uv sync --locked
uv run --locked python main.py
```

The application listens on `http://localhost:8000`. For the complete Grafana, Prometheus, Loki, and Tempo stack, start Docker Compose from the repository root as described in the [root README](../readme.md).
