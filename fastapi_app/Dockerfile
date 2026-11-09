# Same pinned base as the pipelines image: python:3.15-slim-trixie with uv 0.13.6.
FROM ghcr.io/astral-sh/uv:0.13.6-python3.15-trixie-slim@sha256:60a1cfdc8945ea9d12c8fe6221bfa256b2d94df43a93cc957a503346de9ca53b

# Precompile bytecode for faster startup; keep the uv download cache out of the image.
ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /app

# Dependencies first, so code changes reuse this layer. --locked installs exactly
# uv.lock (hash-verified) and fails if it is out of date with pyproject.toml.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# main.py reads its version from pyproject.toml at startup.
COPY main.py utils.py ./

# The runtime user only reads /app: root-owned files stay immutable, and skipping
# chown -R avoids duplicating the whole .venv in an extra image layer.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
USER app

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["python", "main.py"]
