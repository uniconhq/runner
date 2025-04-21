from pathlib import Path

from unicon_runner.executor.base import ExecutorCmd
from unicon_runner.executor.unsafe import UnsafeExecutor
from unicon_runner.models import ComputeContext, Program


class PodmanExecutor(UnsafeExecutor):
    """Uses podman + Dockerfile in template to execute code"""

    def _cmd(self, cwd: Path, _: Program, context: ComputeContext) -> ExecutorCmd:
        # fmt: off
        return [
            "podman", "run", "--rm",
            "-m", f"{context.memory_limit_mb}m",
            "-v", f"{cwd.absolute()}:/run",
            "ghcr.io/astral-sh/uv:debian",
            f"/run/{self.ENTRYPOINT}"
        ], {}
        # fmt: on
