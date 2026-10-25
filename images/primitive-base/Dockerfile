# The primitives' base image, ghcr.io/uniconhq/primitive-base: Python and the
# step wrapper, `unicon-step`, as the entrypoint. A primitive builds from it
# and names its own program as the image's command, `CMD ["/usr/local/bin/<it>"]`;
# in a grading step the wrapper fetches the step's job, runs that command and
# reports, and with no data link it runs the command in its place. The
# pipeline's last step runs this image itself with `mode: report`. Built from
# the repo root: docker build -f images/primitive-base/Dockerfile .

# python:3.14-slim, pulled 2026-09-13. The same digest is in the worker
# Dockerfile, and the primitives built on this one run what compile builds
# with this Python; change them all together.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="primitive-base" \
      org.opencontainers.image.description="The base of Unicon's primitive images: the step wrapper that runs a primitive in a grading step." \
      org.opencontainers.image.source="https://github.com/uniconhq/runner" \
      org.opencontainers.image.licenses="MIT"

COPY --chmod=0755 wrapper/unicon_step.py /usr/local/bin/unicon-step

# /work is where a primitive reads inputs.json and writes outputs.json, which
# any user may write, as a compile step runs as the grading's own user.
# /cache is where a step given the org's cache volume mounts it, root's
# alone: only the primitives that run as root use the cache, and Docker
# copies /cache's owner and mode into a new, empty volume.
RUN mkdir -p /work /cache && chmod 1777 /work && chmod 0755 /cache

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/unicon-step"]
