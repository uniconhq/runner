from pathlib import Path

from jinja2 import Template

from unicon_runner.constants import DEFAULT_EXEC_PY_VERSION
from unicon_runner.executor.base import JINJA_ENV, Executor, ExecutorCmd, FileSystemMapping
from unicon_runner.models import ComputeContext, Program


class UnsafeExecutor(Executor):
    PYPROJECT_TEMPLATE: Template = JINJA_ENV.get_template("pyproject.toml.jinja")
    RUN_SCRIPT_TEMPLATE: Template = JINJA_ENV.get_template("run_unsafe.sh.jinja")

    ENTRYPOINT: Path = Path("run.sh")

    def get_filesystem_mapping(
        self,
        program: Program,
        context: ComputeContext,
        elapsed_time_tracking_files: dict[str, str] | None = None,
    ) -> FileSystemMapping:
        package_dir = Path("src")

        # Assemble all requirements into `requirements.txt` format
        requirements_txt: str = "\n".join(
            (context.extra_options and context.extra_options.requirements) or []
        )

        python_version: str = DEFAULT_EXEC_PY_VERSION
        if context.slurm and context.slurm_use_system_py:
            # NOTE: We need to use the system python interpreter for slurm jobs
            # This is because of filesystem restrictions in the slurm environment (more details in the docs)
            python_version = "/usr/bin/python"
        elif context.extra_options:
            python_version = context.extra_options.version or python_version

        run_script = self.RUN_SCRIPT_TEMPLATE.render(
            python_version=python_version,
            memory_limit_kb=context.memory_limit_mb * 1024,
            time_limit_secs=context.time_limit_secs,
            entry_point=str(package_dir / program.entrypoint),
            track_elapsed_time=elapsed_time_tracking_files is not None,
            **(elapsed_time_tracking_files or {}),
        )

        return [
            *[(package_dir / file.path, file.decoded_data, False) for file in program.files],
            (package_dir / "__init__.py", "", False),
            (Path("pyproject.toml"), self.PYPROJECT_TEMPLATE.render(), False),
            (Path("requirements.txt"), requirements_txt, False),
            (self.ENTRYPOINT, run_script, True),
        ]

    def _cmd(self, cwd: Path, *_unused) -> ExecutorCmd:
        # NOTE: We need to unset VIRTUAL_ENV to prevent uv from using the wrong base python interpreter
        return [str(cwd / self.ENTRYPOINT)], {"VIRTUAL_ENV": "''"}
