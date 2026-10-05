"""A create body as the harness sends it, and the caller it comes from."""

from __future__ import annotations

import copy
from typing import Any

from unicon_filter.config import Config
from unicon_filter.policy import Caller

GRADING = "0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31"
OTHER_GRADING = "0199a2c1-6b7e-7c3a-9f10-000000000000"
HARNESS = "a" * 64
IMAGE = f"ghcr.io/uniconhq/primitive-compile@sha256:{'1' * 64}"
WORKSPACE = "wp_01j9example_default"

CONFIG = Config(images=frozenset({IMAGE}), max_cpus=4.0)
CALLER = Caller(HARNESS, GRADING, WORKSPACE)

MIB = 1024 * 1024


def body() -> dict[str, Any]:
    """What unicon_harness.sandbox.create_body writes for one step."""
    return copy.deepcopy(
        {
            "Image": IMAGE,
            "User": "10001:10001",
            "WorkingDir": "/work",
            "Env": ["HOME=/tmp"],
            "Labels": {
                "unicon.grading": GRADING,
                "unicon.step": "compile",
                "unicon.time_ms": "60000",
            },
            "NetworkDisabled": True,
            "HostConfig": {
                "NetworkMode": "none",
                "Init": False,
                "IpcMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges", "seccomp=builtin"],
                "Memory": 256 * MIB,
                "MemorySwap": 256 * MIB,
                "NanoCpus": 1_000_000_000,
                "PidsLimit": 64,
                "Ulimits": [
                    {"Name": "cpu", "Soft": 60, "Hard": 60},
                    {"Name": "fsize", "Soft": 64 * MIB, "Hard": 64 * MIB},
                ],
                "Mounts": [
                    {
                        "Type": "volume",
                        "Source": WORKSPACE,
                        "Target": "/work",
                        "VolumeOptions": {
                            "Subpath": "unicon-steps/001-compile",
                            "NoCopy": True,
                        },
                    },
                    {
                        "Type": "tmpfs",
                        "Target": "/tmp",
                        "TmpfsOptions": {"SizeBytes": 64 * MIB, "Mode": 0o1777},
                    },
                ],
                "LogConfig": {
                    "Type": "json-file",
                    "Config": {"max-size": "8m", "max-file": "1"},
                },
            },
        }
    )
