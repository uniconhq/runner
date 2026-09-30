"""The filter's settings, all read once at start from its environment. Nothing
can change them while it runs: the image list in particular is fixed for the
filter's life, so nothing a harness sends can add to it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

IMAGE = re.compile(
    r"[a-z0-9][a-z0-9._-]*(:[0-9]{1,5})?(/[a-z0-9][a-z0-9._-]*)+@sha256:[0-9a-f]{64}"
)

IMAGES_VARIABLE = "UNICON_FILTER_IMAGES"
DEFAULT_MAX_TIME_MS = 6 * 60 * 60 * 1000


class ConfigError(Exception):
    """A setting the filter will not start with. `code` is what the one line on
    stderr starts with.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Config:
    images: frozenset[str]
    upstream: str = "/var/run/docker.sock"
    listen: str = "/run/unicon/docker.sock"
    workspace: str = "/woodpecker"
    grace_seconds: float = 30.0
    reap_every_seconds: float = 5.0
    socket_check_seconds: float = 1.0
    max_memory_mb: int = 16384
    max_pids: int = 4096
    max_cpus: float = 8.0
    max_time_ms: int = DEFAULT_MAX_TIME_MS
    max_steps: int = 4
    max_connections: int = 32

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Config:
        """UNICON_FILTER_IMAGES is required: image references by digest,
        separated by commas or white space. The rest have defaults.
        """
        env = os.environ if environment is None else environment
        listed = [i for i in re.split(r"[\s,]+", env.get(IMAGES_VARIABLE, "")) if i]
        if not listed:
            raise ConfigError("missing_environment", f"{IMAGES_VARIABLE} is not set")
        for image in listed:
            if not IMAGE.fullmatch(image):
                raise ConfigError(
                    "bad_setting",
                    f"{IMAGES_VARIABLE} lists {image!r}, which is not an image by "
                    "digest",
                )
        return cls(
            images=frozenset(listed),
            upstream=env.get("UNICON_FILTER_UPSTREAM", cls.upstream),
            listen=env.get("UNICON_FILTER_LISTEN", cls.listen),
            workspace=env.get("UNICON_FILTER_WORKSPACE", cls.workspace),
            grace_seconds=_number(
                env, "UNICON_FILTER_GRACE_SECONDS", cls.grace_seconds
            ),
            reap_every_seconds=_number(
                env, "UNICON_FILTER_REAP_EVERY_SECONDS", cls.reap_every_seconds
            ),
            socket_check_seconds=_number(
                env, "UNICON_FILTER_SOCKET_CHECK_SECONDS", cls.socket_check_seconds
            ),
            max_memory_mb=int(
                _number(env, "UNICON_FILTER_MAX_MEMORY_MB", cls.max_memory_mb)
            ),
            max_pids=int(_number(env, "UNICON_FILTER_MAX_PIDS", cls.max_pids)),
            max_cpus=_number(env, "UNICON_FILTER_MAX_CPUS", float(os.cpu_count() or 1)),
            max_time_ms=int(_number(env, "UNICON_FILTER_MAX_TIME_MS", cls.max_time_ms)),
            max_steps=int(_number(env, "UNICON_FILTER_MAX_STEPS", cls.max_steps)),
            max_connections=int(
                _number(env, "UNICON_FILTER_MAX_CONNECTIONS", cls.max_connections)
            ),
        )


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("bad_setting", f"{name} is {raw!r}, not a number") from None
    if value <= 0:
        raise ConfigError("bad_setting", f"{name} must be above 0")
    return value
