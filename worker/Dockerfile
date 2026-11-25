# Fleet worker image (ADR 0014). No model runtime inside: runners reach a model server at FLEET_MODEL_URL.
# Configured only by environment variables; see fleet_worker/cli.py for the list.
#
#   docker build -t fleet-worker worker

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
# Dependencies first: this layer is reused until uv.lock changes, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
# Then the package itself, installed as a regular (non-editable) copy into the virtualenv.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.11-slim@sha256:61e01f72680f86a5279ad510818ab7b2ee9f049822589d4e34e33e044360356c
# Non-root, fixed uid so volume ownership is predictable (-l: no huge lastlog entry for a high uid).
RUN useradd -l --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin fleet \
    && mkdir /results && chown fleet:fleet /results
# Only the virtualenv: no uv, no sources, no build cache.
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FLEET_RESULTS_DIR=/results
WORKDIR /results
USER 10001
CMD ["fleet-worker"]
