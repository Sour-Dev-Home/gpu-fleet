# Fleet budget kill switch image (ADR 0024): a private Cloud Run service that Pub/Sub pushes budget
# notifications to. It holds no keys: Cloud Run gives it the runtime service account's identity.
#
#   docker build -t fleet-killswitch killswitch

# Base images are pinned by digest (same pins as broker/Dockerfile); Dependabot's docker ecosystem bumps them.
FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

FROM python:3.11-slim@sha256:61e01f72680f86a5279ad510818ab7b2ee9f049822589d4e34e33e044360356c AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.11 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first: this layer is reused until uv.lock changes. --locked: nothing here lives outside
# the build context, so the lock is checked too.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.11-slim@sha256:61e01f72680f86a5279ad510818ab7b2ee9f049822589d4e34e33e044360356c
# Non-root, fixed uid. Writes nothing to disk.
RUN useradd -l --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin fleet
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001
# Cloud Run sets PORT (8080); the server reads it.
EXPOSE 8080
CMD ["python", "-m", "fleet_killswitch"]
