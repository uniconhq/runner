# The socket filter image, ghcr.io/uniconhq/socket-filter: the policy that
# stands between the harness and Docker on a grading machine. It carries a
# placeholder entrypoint; the filter is not written yet. Built from the repo
# root: docker build -f images/socket-filter/Dockerfile .

# python:3.14-slim, pulled 2026-09-13. The same digest is in every Dockerfile
# under images/; change all four together.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="unicon-socket-filter" \
      org.opencontainers.image.description="The Unicon socket filter, the policy between the harness and Docker." \
      org.opencontainers.image.source="https://github.com/uniconhq/runner" \
      org.opencontainers.image.licenses="MIT"

COPY images/placeholder.py /opt/unicon/placeholder.py

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin filter
USER 10001

ENTRYPOINT ["python", "/opt/unicon/placeholder.py", "unicon-socket-filter"]
