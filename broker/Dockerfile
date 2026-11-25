# Fleet broker image (ADR 0014): the HTTP API under uvicorn, SQLite at /data/broker.db.
# Needs FLEET_WORKER_TOKEN and FLEET_ADMIN_TOKEN from the environment (the vault), never from a file.
# The same image adds jobs: docker compose run --rm broker fleet-enqueue --demo N
#
#   docker build -t fleet-broker broker

# Base images are pinned by digest (tags can be moved); Dependabot's docker ecosystem bumps them.
# 3.11: the oldest Python CI tests on. uv 0.13.10: the version CI uses.
FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

FROM python:3.11-slim@sha256:61e01f72680f86a5279ad510818ab7b2ee9f049822589d4e34e33e044360356c AS build
COPY --from=uv /uv /usr/local/bin/uv
# Use the image's Python, never a downloaded one; compile bytecode now so startup is quick;
# copy (not hardlink) files out of the cache mount, which isn't part of the image.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.11 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# --frozen, not --locked: the dev group's fleet-worker comes from ../worker, outside this build
# context, and --locked must read it to check the lock is current. --frozen installs exactly what
# uv.lock says without that check; scripts/check.sh (CI) runs `uv sync --locked`, so a stale lock
# still fails CI before an image is built from it.
# Dependencies first: this layer is reused until uv.lock changes, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
# Then the package itself, installed as a regular (non-editable) copy into the virtualenv.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.11-slim@sha256:61e01f72680f86a5279ad510818ab7b2ee9f049822589d4e34e33e044360356c
# Non-root, fixed uid; /data is created owned by it, so a fresh named volume mounted there is too.
RUN useradd -l --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin fleet \
    && mkdir /data && chown fleet:fleet /data
# Only the virtualenv: no uv, no sources, no build cache.
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FLEET_DB=/data/broker.db
WORKDIR /data
USER 10001
EXPOSE 8000
# 0.0.0.0 inside the container; Compose publishes it on 127.0.0.1 only.
CMD ["uvicorn", "fleet_broker.app:app", "--host", "0.0.0.0", "--port", "8000"]
