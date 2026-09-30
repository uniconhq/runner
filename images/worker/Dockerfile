# The worker image, ghcr.io/uniconhq/worker: the supervisor that sets a grading
# machine up. It carries a placeholder entrypoint; the program is not written
# yet. Built from the repo root: docker build -f images/worker/Dockerfile .

# python:3.14-slim, pulled 2026-09-13. The same digest is in the harness and
# socket-filter Dockerfiles; change all three together.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="unicon-worker" \
      org.opencontainers.image.description="The Unicon worker, the supervisor on a grading machine." \
      org.opencontainers.image.source="https://github.com/uniconhq/runner" \
      org.opencontainers.image.licenses="MIT"

COPY images/placeholder.py /opt/unicon/placeholder.py

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin worker
USER 10001

ENTRYPOINT ["python", "/opt/unicon/placeholder.py", "unicon-worker"]
