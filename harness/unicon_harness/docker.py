"""The harness's Docker client: the seven calls a grading run needs, over the
socket filter's unix socket and never the daemon's own. `Docker` is what the
rest of the harness depends on, so tests stand a fake in its place.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

DOCKER_HOST_VARIABLE = "DOCKER_HOST"

API_VERSION = "v1.45"
"""Volume subpath mounts, which is how a step gets only its own directory of the
run's workspace, arrived in Engine API 1.45 (Docker 26).
"""

REQUEST_TIMEOUT_SECONDS = 30.0


class DockerError(Exception):
    """A Docker call that did not work. `status` is the HTTP status, 0 when the
    socket did not answer; the socket filter's refusals are 403 with its reason
    in `reason`.
    """

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"{status}: {reason}" if status else reason)
        self.status = status
        self.reason = reason


class DockerTimeoutError(DockerError):
    """The socket answered nothing within the time the call allowed."""


class Docker(Protocol):
    def inspect(self, container_id: str) -> dict[str, Any]: ...

    def create(self, body: Mapping[str, Any]) -> str: ...

    def start(self, container_id: str) -> None: ...

    def wait(self, container_id: str, timeout: float) -> int | None:
        """The container's exit code, or None when it is still running after
        `timeout` seconds.
        """
        ...

    def kill(self, container_id: str) -> None: ...

    def logs(self, container_id: str, limit: int) -> str: ...

    def remove(self, container_id: str) -> None: ...


def socket_path(environment: Mapping[str, str]) -> str:
    """The unix socket DOCKER_HOST names. There is no default: a harness that
    fell back to /var/run/docker.sock would be holding the machine.
    """
    host = environment.get(DOCKER_HOST_VARIABLE, "")
    prefix = "unix://"
    if not host.startswith(prefix) or len(host) == len(prefix):
        raise DockerError(
            0,
            f"{DOCKER_HOST_VARIABLE} must name the socket filter as unix:///path, "
            f"not {host!r}",
        )
    return host[len(prefix) :]


class DockerClient:
    """The Engine API over a unix socket, with one connection pool so the socket
    filter sees requests on kept-alive connections the way it is built to.
    """

    def __init__(self, path: str) -> None:
        self._client = httpx.Client(
            transport=httpx.HTTPTransport(uds=path),
            base_url=f"http://docker/{API_VERSION}",
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> DockerClient:
        return cls(socket_path(os.environ if environment is None else environment))

    def close(self) -> None:
        self._client.close()

    def inspect(self, container_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/containers/{container_id}/json")
        document: dict[str, Any] = response.json()
        return document

    def create(self, body: Mapping[str, Any]) -> str:
        response = self._request("POST", "/containers/create", json=dict(body))
        container_id = response.json().get("Id")
        if not isinstance(container_id, str) or not container_id:
            raise DockerError(response.status_code, "create answered without an Id")
        return container_id

    def start(self, container_id: str) -> None:
        self._request("POST", f"/containers/{container_id}/start")

    def wait(self, container_id: str, timeout: float) -> int | None:
        if timeout <= 0:
            return None
        try:
            response = self._request(
                "POST",
                f"/containers/{container_id}/wait",
                params={"condition": "not-running"},
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, read=timeout),
            )
        except DockerTimeoutError:
            return None
        status = response.json().get("StatusCode")
        if not isinstance(status, int):
            raise DockerError(
                response.status_code, "wait answered without a StatusCode"
            )
        return status

    def kill(self, container_id: str) -> None:
        """SIGKILL. A container that already stopped is not an error: killing is
        how the wall clock is enforced, and the step may have won the race.
        """
        try:
            self._request("POST", f"/containers/{container_id}/kill")
        except DockerError as exc:
            if exc.status != 409:
                raise

    def logs(self, container_id: str, limit: int) -> str:
        """What the container printed, stdout and stderr in order, at most `limit`
        bytes of it.
        """
        data = bytearray()
        with self._client.stream(
            "GET",
            f"/containers/{container_id}/logs",
            params={"stdout": "1", "stderr": "1"},
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise DockerError(response.status_code, _reason(response))
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) >= limit + 8:
                    break
        multiplexed = "multiplexed" in response.headers.get("content-type", "")
        text = _demultiplexed(bytes(data)) if multiplexed else bytes(data)
        return text[:limit].decode("utf-8", errors="replace")

    def remove(self, container_id: str) -> None:
        """Force-remove. A container already gone is not an error."""
        try:
            self._request(
                "DELETE", f"/containers/{container_id}", params={"force": "1"}
            )
        except DockerError as exc:
            if exc.status != 404:
                raise

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise DockerTimeoutError(0, f"{method} {path} timed out") from exc
        except httpx.TransportError as exc:
            raise DockerError(
                0,
                f"the Docker socket did not answer {method} {path}: "
                f"{type(exc).__name__}",
            ) from exc
        if response.status_code >= 400:
            raise DockerError(response.status_code, _reason(response))
        return response


def _reason(response: httpx.Response) -> str:
    """The daemon's message, or the socket filter's reason for a refusal."""
    try:
        message = json.loads(response.content).get("message")
    except ValueError, AttributeError:
        message = None
    if isinstance(message, str) and message:
        return message
    return response.reason_phrase or f"HTTP {response.status_code}"


def _demultiplexed(data: bytes) -> bytes:
    """The payload of Docker's multiplexed log stream: frames of an eight-byte
    header, whose last four bytes are the length, followed by that many bytes.
    """
    out = bytearray()
    position = 0
    while position + 8 <= len(data):
        size = int.from_bytes(data[position + 4 : position + 8], "big")
        out.extend(data[position + 8 : position + 8 + size])
        position += 8 + size
    return bytes(out)
