# One image for the six services (Claims API, Agent Runtime, Model Gateway and
# the policy, claims and knowledge tool servers) and for `meridian db migrate`,
# `meridian db seed-policies` and `meridian knowledge ingest`. This file sets no
# CMD or ENTRYPOINT (the base image's own CMD, `python3`, is inherited); each
# manifest names its command.
# Built by infra/kind/deploy.sh (`make deploy`); S019/S021 take over the build.
#
# Base images are pinned by their multi-arch index digest. Looked up
# 2026-10-01 with `docker buildx imagetools inspect`:
#   python:3.13-slim  = 3.13.15-slim-trixie
#   uv 0.12.21        = the newest 0.12 release (pyproject asks uv_build 0.12)

FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS build
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

FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS runtime
RUN groupadd --gid 10001 meridian \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin meridian
# Same base image and same path as the build stage: the venv's interpreter
# link and scripts keep working.
COPY --from=build /opt/venv /opt/venv
COPY config/registry /opt/meridian/registry
# The seed data `meridian db seed-policies` and `meridian knowledge ingest` read,
# and nothing else of data/synthetic: not the claims, the expected outcomes or
# the generator (the manifest holds their hashes and counts). The policies file
# names each synthetic holder; only the seed Job reads it (T-51).
# .dockerignore lets the same files into the build context.
COPY data/synthetic/manifest.json data/synthetic/policies.json data/synthetic/claim-history.json /opt/meridian/synthetic/
COPY data/synthetic/wordings/*.md /opt/meridian/synthetic/wordings/
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MERIDIAN_REGISTRY_DIR=/opt/meridian/registry \
    OTEL_PYTHON_FASTAPI_EXCLUDED_URLS=/healthz
USER 10001:10001
