# One image for the three services (Claims API, Agent Runtime, Model Gateway)
# and for `meridian db migrate`. This file sets no CMD or ENTRYPOINT (the base
# image's own CMD, `python3`, is inherited); each manifest names its command.
# Built by infra/kind/deploy.sh (`make deploy`); S019/S021 take over the build.
#
# Base images are pinned by their multi-arch index digest. Looked up
# 2026-10-01 with `docker buildx imagetools inspect`:
#   python:3.13-slim  = 3.13.15-slim-trixie
#   uv 0.12.21        = the newest 0.12 release (pyproject asks uv_build 0.12)

FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS build
COPY --from=uv /uv /usr/local/bin/uv
# The system Python of this base image is the only interpreter: uv downloads
# none. Bytecode is compiled here, so the final image never needs to write it.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
# Non-editable: the package is installed into site-packages, not linked to /src.
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS runtime
RUN groupadd --gid 10001 meridian \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin meridian
# Same base image and same path as the build stage: the venv's interpreter
# link and scripts keep working.
COPY --from=build /opt/venv /opt/venv
COPY config/registry /opt/meridian/registry
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MERIDIAN_REGISTRY_DIR=/opt/meridian/registry \
    OTEL_PYTHON_FASTAPI_EXCLUDED_URLS=/healthz
USER 10001:10001
