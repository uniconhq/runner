# The clone image, ghcr.io/uniconhq/clone: what the CI checks code out with. It
# carries a placeholder entrypoint; the clone plugin and the big-file setting
# are not in it yet. Built from the repo root:
# docker build -f images/clone/Dockerfile .

# python:3.14-slim, pulled 2026-09-13. The same digest is in every Dockerfile
# under images/; change all four together.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="unicon-clone" \
      org.opencontainers.image.description="The Unicon clone image, what the CI checks code out with." \
      org.opencontainers.image.source="https://github.com/uniconhq/runner" \
      org.opencontainers.image.licenses="MIT"

COPY images/placeholder.py /opt/unicon/placeholder.py

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin clone
USER 10001

ENTRYPOINT ["python", "/opt/unicon/placeholder.py", "unicon-clone"]
