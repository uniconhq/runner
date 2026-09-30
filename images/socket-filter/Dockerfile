# The socket filter image, ghcr.io/uniconhq/socket-filter: the policy that
# stands between the harness and Docker on a grading machine. Standard-library
# Python, so the image is the base and one directory. Built from the repo root:
# docker build -f images/socket-filter/Dockerfile .
#
# Run it with the daemon's socket mounted at /var/run/docker.sock and the
# socket's group added (or as root), its own socket's directory at
# /run/unicon, UNICON_FILTER_IMAGES set, and `pid: host`: the filter tells one
# grading run from another by the process that connects, which it can only see
# in the machine's pid namespace.
#
# It runs as uid 10002, which no harness (10001) or step runs as, and
# /run/unicon belongs to it, so a new volume mounted there does too. The
# socket is mode 0666, so the harness can connect; the harness mounts the
# directory read-only, so it cannot put a socket of its own in the filter's
# place, and the filter exits 3 should the socket ever be replaced.

# python:3.14-slim, pulled 2026-09-13. The same digest is in the harness and
# worker Dockerfiles; change all three together.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="unicon-socket-filter" \
      org.opencontainers.image.description="The Unicon socket filter, the policy between the harness and Docker." \
      org.opencontainers.image.source="https://github.com/uniconhq/runner" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONPATH=/opt/unicon \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY socket_filter/unicon_filter /opt/unicon/unicon_filter

RUN groupadd --gid 10002 filter \
    && useradd --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin filter \
    && mkdir -p /run/unicon && chown 10002:10002 /run/unicon && chmod 0755 /run/unicon
USER 10002:10002

ENTRYPOINT ["python", "-m", "unicon_filter"]
