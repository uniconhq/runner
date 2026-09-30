# The harness image, ghcr.io/uniconhq/harness. The CI starts one of these per
# grading run; it runs the plan by starting one sandboxed container per step
# through the socket filter. Built from the repo root:
# docker build -f images/harness/Dockerfile .

# Pinned by digest so a published task keeps grading the way it was published.
# Bump it in its own commit, with the digest from
# `docker image inspect python:3.14-slim --format '{{index .RepoDigests 0}}'`.
# python:3.14-slim, pulled 2026-09-13. The same line is in the worker and
# socket-filter Dockerfiles; change all three together.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS build

# uv 0.12.13, by the digest of its multi-platform index.
COPY --from=ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/unicon-harness

WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY harness/unicon_harness ./harness/unicon_harness
COPY schemas ./schemas

# --no-editable so the schemas are copied into site-packages rather than left
# pointing at /src, which the runtime stage does not have.
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="unicon-harness" \
      org.opencontainers.image.description="The Unicon grading harness." \
      org.opencontainers.image.source="https://github.com/uniconhq/runner" \
      org.opencontainers.image.licenses="MIT"

COPY --from=build /opt/unicon-harness /opt/unicon-harness
ENV PATH="/opt/unicon-harness/bin:${PATH}"

# No USER line. The CI's workspace volume belongs to root, because the clone
# steps run as root, so the harness starts as root only to make its step
# directory there; it then gives that directory to uid 10001 and becomes
# 10001 for the rest of the run, before it reads the plan or the submission.
# Every step container runs as 10001 too.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin harness

ENTRYPOINT ["unicon-harness"]
