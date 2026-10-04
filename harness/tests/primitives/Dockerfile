# The fixture primitives, and the stand-in platform server, as an image for
# the Docker integration tests only. The entrypoint is the fixture, which tells
# from a step's inputs which primitive it stands in for, so every step of a
# plan can run this one image.
# Built from this directory: docker build harness/tests/primitives

FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ENV PYTHONDONTWRITEBYTECODE=1
COPY fixture.py platform_server.py /opt/
USER 65532
ENTRYPOINT ["python", "/opt/fixture.py"]
