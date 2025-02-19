import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import Annotated

import pika
import pika.spec
import typer
from pika.adapters.blocking_connection import BlockingChannel
from pika.exchange_type import ExchangeType
from rich.logging import RichHandler

from unicon_runner.executor import create_executor
from unicon_runner.executor.base import Executor, ExecutorType, ProgramResult
from unicon_runner.models import Job, JobResult, Program

logging.basicConfig(
    level="INFO",
    format="[magenta]%(funcName)s[/] - %(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(markup=True)],
)
logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("pika").setLevel(logging.WARN)

logger = logging.getLogger("unicon_runner")

app = typer.Typer(name="Unicon 🦄 Runner")


async def _run_job_async(executor: Executor, job: Job) -> JobResult:
    _tracking_fields = job.model_extra or {}
    async with asyncio.TaskGroup() as tg:
        program_tasks: list[asyncio.Task[ProgramResult]] = [
            tg.create_task(executor.run(program, job.context)) for program in job.programs
        ]
    program_results = [program_task.result() for program_task in program_tasks]
    return JobResult(success=True, error=None, results=program_results, **_tracking_fields)


def _run_job(executor: Executor, job: Job) -> JobResult:
    compatible, reason = executor.is_compatible(job.context)
    if not compatible:
        _tracking_fields = job.model_extra or {}
        return JobResult(success=False, error=reason, results=[], **_tracking_fields)
    return asyncio.run(_run_job_async(executor, job))


def exec_pipeline(
    in_ch: BlockingChannel,
    method: pika.spec.Basic.Deliver,
    _: pika.spec.BasicProperties,
    msg_body: bytes,
    out_ch: BlockingChannel,
    executor: Executor,
) -> None:
    from unicon_runner.constants import AMQP_EXCHANGE_NAME, AMQP_RESULT_QUEUE_NAME

    logger.info(f"Received message: {len(msg_body)} bytes")
    job = Job.model_validate_json(msg_body)
    logger.info(f"Received job: {job.model_extra}")

    result = _run_job(executor, job)

    logger.info(f"Pushing result: {result.model_extra}")
    out_ch.basic_publish(AMQP_EXCHANGE_NAME, AMQP_RESULT_QUEUE_NAME, result.model_dump_json())

    if not result.success:
        # If the job failed to run, only requeue if it has not been redelivered
        in_ch.basic_nack(delivery_tag=method.delivery_tag, requeue=not method.redelivered)
    else:
        in_ch.basic_ack(delivery_tag=method.delivery_tag)


def init_mq() -> tuple[BlockingChannel, BlockingChannel]:
    from unicon_runner.constants import (
        AMQP_CONN_NAME,
        AMQP_EXCHANGE_NAME,
        AMQP_RESULT_QUEUE_NAME,
        AMQP_TASK_QUEUE_NAME,
        AMQP_URL,
    )

    if AMQP_URL is None:
        raise RuntimeError("RABBITMQ_URL environment variable not defined")

    conn_params = pika.URLParameters(AMQP_URL)
    conn_params.client_properties = {"connection_name": AMQP_CONN_NAME}
    conn = pika.BlockingConnection(conn_params)

    in_ch, out_ch = conn.channel(), conn.channel()
    for ch in [in_ch, out_ch]:
        ch.exchange_declare(exchange=AMQP_EXCHANGE_NAME, exchange_type=ExchangeType.topic)

    in_ch.queue_declare(queue=AMQP_TASK_QUEUE_NAME, durable=True)
    in_ch.queue_bind(AMQP_TASK_QUEUE_NAME, AMQP_EXCHANGE_NAME, AMQP_TASK_QUEUE_NAME)

    out_ch.queue_declare(queue=AMQP_RESULT_QUEUE_NAME, durable=True)
    out_ch.queue_bind(AMQP_RESULT_QUEUE_NAME, AMQP_EXCHANGE_NAME, AMQP_RESULT_QUEUE_NAME)

    return in_ch, out_ch


RootWorkingDirectory = Annotated[
    Path,
    typer.Argument(
        exists=True,
        writable=True,
        readable=True,
        help="Root path for executor's working directory",
    ),
]


@app.callback()
def main():
    """Unicon 🦄 Runner"""


@app.command()
def start(exec_type: ExecutorType, root_wd_dir: RootWorkingDirectory) -> None:
    from unicon_runner.constants import AMQP_TASK_QUEUE_NAME

    """Starts the unicon-runner service"""
    in_ch, out_ch = init_mq()
    logger.info("Initialized task and result queues")

    executor = create_executor(exec_type, root_wd_dir)
    logger.info(f"Created executor: [bold green]{executor.__class__.__name__}[/]")
    logger.info(f"Root working directory: [bold green]{root_wd_dir.absolute()}[/]")

    in_ch.basic_qos(prefetch_count=1)
    in_ch.basic_consume(
        AMQP_TASK_QUEUE_NAME,
        on_message_callback=partial(exec_pipeline, out_ch=out_ch, executor=executor),
        auto_ack=False,
    )

    try:
        in_ch.start_consuming()  # NOTE: This is a blocking call (runs forever)
    except KeyboardInterrupt:
        in_ch.stop_consuming()


@app.command()
def test(
    exec_type: ExecutorType,
    root_wd_dir: RootWorkingDirectory,
    job_file: Annotated[Path, typer.Argument(exists=True, readable=True)],
    slurm: bool | None = None,
    slurm_opt: list[str] | None = None,
    slurm_use_system_py: bool | None = None,
    exec_py_version: str | None = None,
) -> None:
    """Test executors"""
    from unicon_runner.models import ExtraOptions

    job = Job.model_validate_json(job_file.read_bytes())
    job.context.slurm = slurm or job.context.slurm
    job.context.slurm_options = slurm_opt or job.context.slurm_options
    job.context.slurm_use_system_py = slurm_use_system_py or job.context.slurm_use_system_py

    job.context.extra_options = job.context.extra_options or ExtraOptions()
    job.context.extra_options.version = exec_py_version or job.context.extra_options.version

    executor = create_executor(exec_type, root_wd_dir)

    from rich.console import Console
    from rich.table import Table

    _console = Console()

    async def _run_job(program: Program) -> ProgramResult:
        # Since this is a test, we don't want to clean up the working directory
        # This is so that we can easily inspect the files written and replay the execution
        return await executor.run(program, job.context, cleanup=False, track_elapsed_time=True)

    for i, program in enumerate(job.programs):
        prog_result = asyncio.run(_run_job(program))
        assert prog_result.elapsed_time_ns is not None

        tbl = Table(title=f"Program Result #{i + 1}", highlight=True)
        tbl.add_column("status", style="magenta")
        tbl.add_column("stdout")
        tbl.add_column("stderr", style="red")
        tbl.add_column("Elapsed Time (ms)", style="green")
        tbl.add_row(
            prog_result.status,
            prog_result.stdout,
            prog_result.stderr,
            f"{prog_result.elapsed_time_ns / 1e6:.4f}ms",
        )
        _console.print(tbl)
