from pathlib import Path

from unicon_runner.constants import DEFAULT_EXEC_PY_VERSION
from unicon_runner.executor.base import Executor, ExecutorCmd, FileSystemMapping
from unicon_runner.models import ComputeContext, Program


class PodmanExecutor(Executor):
    """Uses podman + Dockerfile in template to execute code"""

    def get_filesystem_mapping(self, program: Program, *_unused) -> FileSystemMapping:
        return [(Path(file.path), file.decoded_data, False) for file in program.files]

    def _cmd(self, cwd: Path, program: Program, context: ComputeContext) -> ExecutorCmd:
        # fmt: off
        return [
            "podman", "run", "--rm",
            "-m", f"{context.memory_limit_mb}m",
            "-v", f"{cwd.absolute()}:/run",
            f"python:{DEFAULT_EXEC_PY_VERSION}",
            "timeout", "--verbose", f"{context.time_limit_secs}s", "python", f"/run/{program.entrypoint}",
        ], {}
        # fmt: on
