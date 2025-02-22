import asyncio
import logging
import os
import shlex
import shutil
import stat
import uuid
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Final

import psutil
from jinja2 import Environment, PackageLoader, select_autoescape

from unicon_runner.constants import DEFAULT_SLURM_OPTS
from unicon_runner.models import (
    ComputeContext,
    ExecutorPerf,
    ExecutorResult,
    Program,
    ProgramResult,
    Status,
)

logger = logging.getLogger("unicon_runner")

JINJA_ENV = Environment(
    loader=PackageLoader("unicon_runner.executor"), autoescape=select_autoescape()
)


def is_mounted_on_nfs(path: Path) -> bool:
    """Check if the given path is mounted on an NFS filesystem"""
    dev_no: int = os.stat(path).st_dev
    # `fstype` for NFS filesystems is usually `nfs` or `nfs4`
    # `nfs` is for NFSv2 and NFSv3
    nfs_partitions = [p for p in psutil.disk_partitions(all=True) if p.fstype.startswith("nfs")]
    return any(os.lstat(nfs_p.mountpoint).st_dev == dev_no for nfs_p in nfs_partitions)


class ExecutorWorkspace:
    def __init__(self, root_dir: Path, id: str, cleanup: bool):
        self._cwd = root_dir / id
        self._cwd.mkdir(parents=True)

        self._cleanup = cleanup

    def __enter__(self):
        return self._cwd

    def __exit__(self, type, value, traceback):
        if self._cleanup and (type, value, traceback) == (None, None, None):
            # Only clean up if there was no exception when exiting the context
            # and if the workspace set to be cleaned up
            shutil.rmtree(self._cwd)


# [(<file_path>, <file_content>, <is_executable>)]
FileSystemMapping = list[tuple[Path, str | bytes, bool]]

# (cmd, env_vars)
ExecutorCmd = tuple[list[str], dict[str, str]]


class ExecutorType(str, Enum):
    PODMAN = "podman"
    UNSAFE = "unsafe"
    SANDBOX = "sandbox"


# NOTE: The keys corresponds to the jinja template variables
TIME_TRACKING_FILES: Final[dict[str, str]] = {
    "create_venv_time_file": ".create_venv_time_ns",
    "install_deps_time_file": ".install_deps_time_ns",
    "program_time_file": ".program_time_ns",
}


def collect_perf_results(root: Path) -> ExecutorPerf:
    def get_time_ns(file_path: Path) -> int:
        return int(file_path.read_text()) if file_path.read_text() else 0

    return ExecutorPerf(
        create_venv_ns=get_time_ns(root / TIME_TRACKING_FILES["create_venv_time_file"]),
        install_deps_ns=get_time_ns(root / TIME_TRACKING_FILES["install_deps_time_file"]),
        program_ns=get_time_ns(root / TIME_TRACKING_FILES["program_time_file"]),
    )


class Executor(ABC):
    def __init__(self, root_dir: Path):
        self._root_dir = root_dir

    @abstractmethod
    def get_filesystem_mapping(
        self,
        program: Program,
        context: ComputeContext,
        elapsed_time_tracking_files: dict[str, str] | None = None,
    ) -> FileSystemMapping:
        """
        Mapping of files (path, content) to be written to the working directory of the executor
        """
        raise NotImplementedError

    @abstractmethod
    def _cmd(self, cwd: Path, program: Program, context: ComputeContext) -> ExecutorCmd:
        raise NotImplementedError

    async def _collect(self, proc: asyncio.subprocess.Process) -> ExecutorResult:
        stdout, stderr = await proc.communicate()
        exit_code = proc.returncode if proc.returncode is not None else 1
        return ExecutorResult(exit_code=exit_code, stdout=stdout.decode(), stderr=stderr.decode())

    def is_compatible(self, context: ComputeContext) -> tuple[bool, str]:
        # NOTE: We assume that as long as the working directory is on **any** NFS,
        # all nodes in the cluster will have access to it
        if context.slurm and not is_mounted_on_nfs(self._root_dir):
            return False, "Cannot run slurm job as working directory is not on NFS"
        return True, ""

    async def run(
        self,
        program: Program,
        context: ComputeContext,
        cleanup: bool = True,
        track_elapsed_time: bool = True,
    ) -> ProgramResult:
        _tracking_fields = program.model_extra or {}
        id: str = str(uuid.uuid4())  # Unique identifier for the program
        with ExecutorWorkspace(self._root_dir, id, cleanup) as workspace:
            for path, content, is_exec in self.get_filesystem_mapping(
                program, context, TIME_TRACKING_FILES if track_elapsed_time else None
            ):
                logger.info(f"Writing file: [magenta]{path}[/]")
                file_path = workspace / path
                file_path.parent.mkdir(parents=True, exist_ok=True)

                if isinstance(content, bytes):
                    file_path.write_bytes(content)
                else:
                    file_path.write_text(content)

                if is_exec:
                    file_path.chmod(file_path.stat().st_mode | stat.S_IEXEC)

            cmd: list[str]
            env_vars: dict[str, str]

            if context.slurm:
                # NOTE: For `slurm` execution, the working directory needs to be in NFS
                # There will be 2 working directories:
                # 1. NFS working directory (where the setup is done)
                # 2. Slurm working directory (where the program is executed)
                #   - This is hardcoded to `/tmp` for now
                #   - A possible improvement is to introduce staging and execution directories
                exec_dir = Path("/tmp") / id
                prog_cmd, prog_env_vars = self._cmd(exec_dir, program, context)
                logger.info(f"Program command: {prog_cmd}")

                # Assemble the script that copies files from NFS to Slurm working directory
                slurm_script = JINJA_ENV.get_template("slurm.sh.jinja").render(
                    staging_dir=str(workspace),
                    exec_dir=str(exec_dir),
                    exec_export_env_vars="\n".join(
                        [f"export {key}={value}" for key, value in prog_env_vars.items()]
                    ),
                    run_script=shlex.join(prog_cmd),
                    files_to_preserve=list(TIME_TRACKING_FILES.values())
                    if track_elapsed_time
                    else [],
                )
                slurm_script_path = workspace / "slurm.sh"
                slurm_script_path.write_text(slurm_script)
                slurm_script_path.chmod(slurm_script_path.stat().st_mode | stat.S_IEXEC)

                # NOTE: `--quiet` flag is used to suppress informational messages from `srun` being logged into stderr
                # Errors will NOT be suppressed
                slurm_options: list[str] = [DEFAULT_SLURM_OPTS, *context.slurm_options]
                cmd = ["srun", "--quiet", *slurm_options, str(slurm_script_path)]
                env_vars = {}
            else:
                cmd, env_vars = self._cmd(workspace, program, context)

            logger.info(f"Process command: {cmd}")
            logger.info(f"Env variables: {env_vars}")

            result = await self._collect(
                await asyncio.create_subprocess_shell(
                    shlex.join(cmd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, **env_vars},
                )
            )

            if perf := (collect_perf_results(workspace) if track_elapsed_time else None):
                logger.info(f"[Setup] Create venv: {perf.create_venv_ns / 1e6:.4f}ms")
                logger.info(f"[Setup] Install deps: {perf.install_deps_ns / 1e6:.4f}ms")
                logger.info(f"[Program] Elapsed time: {perf.program_ns / 1e6:.4f}ms")

        match result.exit_code:
            case 137:
                status = Status.MLE
            case 124:
                status = Status.TLE
            case 1:
                status = Status.RTE
            case _:
                status = Status.OK

        return ProgramResult.model_validate(
            {
                **_tracking_fields,
                "status": status.value,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "elapsed_time_ns": perf.program_ns if perf else None,
            }
        )
