import asyncio
import logging
import os
import random
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from opentelemetry.propagate import inject
from utils import (LOG_FORMAT, PrometheusMiddleware, metrics, project_version,
                   setting_otlp)

APP_NAME = os.environ.get("APP_NAME", "app")
APP_VERSION = project_version(Path(__file__).with_name("pyproject.toml"))
EXPOSE_PORT = int(os.environ.get("EXPOSE_PORT", "8000"))
OTLP_GRPC_ENDPOINT = os.environ.get("OTLP_GRPC_ENDPOINT", "tempo:4317")
OTLP_INSECURE = os.environ.get("OTLP_INSECURE", "false").lower() == "true"

TARGET_ONE_HOST = os.environ.get("TARGET_ONE_HOST", "app-b")
TARGET_TWO_HOST = os.environ.get("TARGET_TWO_HOST", "app-c")
HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

for target_name, target_host in (
    ("TARGET_ONE_HOST", TARGET_ONE_HOST),
    ("TARGET_TWO_HOST", TARGET_TWO_HOST),
):
    if not HOSTNAME_PATTERN.fullmatch(target_host):
        raise ValueError(
            f"{target_name} must be a hostname; received {target_host!r}"
        )


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    yield
    await application.state.http_client.aclose()

app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def add_app_version_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-App-Version"] = APP_VERSION
    return response

# Setting metrics middleware
app.add_middleware(PrometheusMiddleware, app_name=APP_NAME)
app.add_route("/metrics", metrics)

# Setting OpenTelemetry exporter
setting_otlp(app, APP_NAME, APP_VERSION, OTLP_GRPC_ENDPOINT, insecure=OTLP_INSECURE)


class EndpointFilter(logging.Filter):
    # Uvicorn endpoint access log filter
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find("GET /metrics") == -1


# Filter out /endpoint
logging.getLogger("uvicorn.access").addFilter(EndpointFilter())


@app.get("/")
async def read_root():
    logging.info("Hello World")
    return {"Hello": "World"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/items/{item_id}")
async def read_item(item_id: int, q: Optional[str] = None):
    logging.info("items")
    return {"item_id": item_id, "q": q}


@app.get("/io_task")
async def io_task():
    await asyncio.sleep(1)
    logging.info("io task")
    return "IO bound task finish!"


@app.get("/cpu_task")
async def cpu_task():
    await asyncio.to_thread(sum, (number * number * number for number in range(1000)))
    logging.info("cpu task")
    return "CPU bound task finish!"


@app.get("/random_status")
async def random_status(response: Response):
    response.status_code = random.choice([200, 200, 202, 400, 500])
    logging.info("random status")
    return {"path": "/random_status"}


@app.get("/random_sleep")
async def random_sleep(response: Response):
    await asyncio.sleep(random.randint(0, 5))
    logging.info("random sleep")
    return {"path": "/random_sleep"}


@app.get("/error_test")
async def error_test(response: Response):
    logging.error("Intentional error test")
    raise ValueError("value error")


@app.get("/chain")
async def chain(request: Request):
    headers = {}
    inject(headers)  # inject trace info to header
    logging.debug("Injected trace context into downstream requests")

    try:
        responses = await asyncio.gather(
            request.app.state.http_client.get(
            "http://localhost:8000/",
            headers=headers,
            ),
            request.app.state.http_client.get(
            f"http://{TARGET_ONE_HOST}:8000/io_task",
            headers=headers,
            ),
            request.app.state.http_client.get(
            f"http://{TARGET_TWO_HOST}:8000/cpu_task",
            headers=headers,
            ),
        )
        for downstream_response in responses:
            downstream_response.raise_for_status()
    except httpx.HTTPError as error:
        logging.error("Chain dependency failed: %s", error)
        raise HTTPException(status_code=502, detail="Chain dependency failed") from error

    logging.info("Chain Finished")
    return {"path": "/chain"}


if __name__ == "__main__":
    # update uvicorn access logger format
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = LOG_FORMAT
    uvicorn.run(app, host="0.0.0.0", port=EXPOSE_PORT, log_config=log_config)
