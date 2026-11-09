FROM ghcr.io/astral-sh/uv:0.13.6-python3.15-trixie-slim@sha256:60a1cfdc8945ea9d12c8fe6221bfa256b2d94df43a93cc957a503346de9ca53b

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

# The runtime user only reads /app: root-owned files stay immutable, and skipping
# chown -R avoids duplicating the whole .venv in an extra image layer.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
USER app

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["pipelines"]
