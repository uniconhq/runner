import abc
from collections import deque
from enum import Enum
from itertools import groupby
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from unicon_runner.executor.variants.base import Executor, ExecutorResult
from unicon_runner.lib.common import CustomBaseModel
from unicon_runner.schemas import (
    File,
    ProgrammingEnvironment,
    Request,
    TaskEvalResult,
    TaskEvalStatus,
)


class StepType(str, Enum):
    PY_RUN_FUNCTION = "PY_RUN_FUNCTION_STEP"
    STRING_MATCH = "STRING_MATCH_STEP"
    INPUT = "INPUT_STEP"
    OUTPUT = "OUTPUT_STEP"
    CONSTANT = "CONSTANT_STEP"


StepInputType = TypeVar("StepInputType")
StepExpectedAnswer = TypeVar("StepExpectedAnswer")
StepOutputType = TypeVar("StepOutputType")

type Unused = None


class Socket(BaseModel):
    id: int
    name: str


class Step(
    CustomBaseModel,
    abc.ABC,
    Generic[StepInputType, StepExpectedAnswer, StepOutputType],
    polymorphic=True,
):
    id: int
    type: StepType
    inputs: list[Socket]
    outputs: list[Socket]

    def get_comment_header(self):
        return f"# Step {self.id}: {self.type.value}"

    @abc.abstractmethod
    def get_code(self, inputs: dict):
        pass


class ConstantStep(Step[Unused, Unused, dict]):
    values: dict

    def get_code(self, *__unused_args):
        code = [self.get_comment_header()]
        for output in self.outputs:
            value = self.values[output.name]
            code.append(f"var_{self.id}_{output.id} = {value}")
        return "\n".join(code)


class InputStep(Step[Unused, Unused, dict]):
    values: dict

    def get_code(self, *__unused_args):
        code = [self.get_comment_header()]
        for output in self.outputs:
            value = self.values[output.name]
            code.append(f"var_{self.id}_{output.id} = {value}")
        return "\n".join(code)


class OutputStep(Step[dict, Unused, dict]):
    def get_code(self, inputs: dict):
        code = [self.get_comment_header(), "import json"]
        result = (
            "{"
            + ", ".join((f'"{key}": {variable_name}' for key, variable_name in inputs.items()))
            + "}"
        )
        print_statement = f"print(json.dumps({result}))"
        code.append(print_statement)
        return "\n".join(code)


class StringMatchStep(Step[str, str, bool]):
    def get_code(self, inputs: dict):
        output = self.outputs[0]
        output_variable = f"var_{self.id}_{output.id}"
        input_variables = list(inputs.values())
        assert len(input_variables) == 2
        code = [self.get_comment_header()]
        code.append(f"{output_variable} = str({input_variables[0]}) == str({input_variables[1]})")
        return "\n".join(code)


class Params(BaseModel):
    arguments: list[int | str]
    keyword_arguments: dict[str, str]


class PyRunFunctionStep(Step[list[File], Unused, ExecutorResult]):
    file_name: str
    function_name: str
    params: Params | None = None
    user_input: list[File] | None = []

    def set_user_input(self, user_input: list[File]):
        self.user_input = user_input

    def get_code(self, inputs: dict):
        # Precondition: set_user_input has been called
        assert self.user_input != None

        if not any(f.file_name == self.file_name for f in self.user_input):
            raise ValueError(f"File {self.file_name} not found in input files")

        assert len(inputs) == 1
        params_variable = list(inputs.values())[0]

        func_invocation = f"{self.function_name}(*{params_variable}['arguments'], **{params_variable}['keyword_arguments'])"
        # TODO: Remove dependence on `print` and `stdout`
        output = self.outputs[0]
        output_variable = f"var_{self.id}_{output.id}"
        assembled_code = f"from {self.file_name.split(".py")[0]} import {self.function_name}\n\n{output_variable} = {func_invocation}"
        return self.get_comment_header() + "\n" + assembled_code


class ProgrammingTaskExpectedAnswer(BaseModel):
    testcase_id: int
    step_id: int
    expected_answer: Any


class Link(BaseModel):
    id: int
    from_node_id: int
    from_socket_id: int
    to_node_id: int
    to_socket_id: int


class Graph(BaseModel):
    steps: list[Step]
    links: list[Link]

    async def run(
        self, user_input: list[File], environment: ProgrammingEnvironment, executor: Executor
    ) -> dict:
        # for lookup
        step_map = {step.id: step for step in self.steps}
        link_map = {
            id: [link for link in self.links if link.from_node_id == id]
            for id in set(link.from_node_id for link in self.links)
        }

        ready_queue = deque[tuple[Step, dict]]()
        forming_map = {}

        # initialisation step: move all nodes with no inputs to ready queue
        for step in self.steps:
            if not step.inputs:
                ready_queue.append((step, {}))

        script_parts = []

        while True:
            if not ready_queue:
                raise ValueError("Ended without reaching output node.")

            current_step, inputs = ready_queue.popleft()

            if current_step.type == StepType.PY_RUN_FUNCTION:
                current_step.set_user_input(user_input)

            script_part = current_step.get_code(inputs)
            script_parts.append(script_part)

            if current_step.type == StepType.OUTPUT:
                break

            # update forming map
            for outgoing_link in link_map[current_step.id]:
                value = f"var_{outgoing_link.from_node_id}_{outgoing_link.from_socket_id}"

                if not forming_map.get(outgoing_link.to_node_id):
                    ingoing_step = step_map[outgoing_link.to_node_id]
                    forming_map[outgoing_link.to_node_id] = (ingoing_step, {})

                ingoing_step = step_map[outgoing_link.to_node_id]
                ingoing_key = [
                    input for input in ingoing_step.inputs if input.id == outgoing_link.to_socket_id
                ][0].name

                forming_map[outgoing_link.to_node_id][1][ingoing_key] = value

            # move formed nodes to ready_queue
            for id, (step, params) in list(forming_map.items()):
                if len(step.inputs) == len(params):
                    ready_queue.append((step, params))
                    del forming_map[id]

        script = "\n\n".join(script_parts)

        return await executor.run_request(
            request=Request(
                files=user_input + [File(file_name="__run.py", content=script)],
                environment=environment,
                entrypoint="__run.py",
            ),
            request_id=str(uuid4()),
        )


# class LoopStep(Step):
#     subgraph: Graph

#     def set_user_input(self, user_input: list[File]):
#         self.user_input = user_input

#     async def run(
#         self,
#         inputs: dict,
#         environment: ProgrammingEnvironment,
#         executor: Executor,
#     ):
#         state = inputs.get("state")
#         while True:
#             outputs = await self.subgraph.run(state, environment, executor)
#             if outputs["end"]:
#                 break
#         return outputs


class Testcase(BaseModel):
    id: int
    steps: list[Step]
    links: list[Link]

    async def run(
        self,
        user_input: list[File],
        expected_answer: list[ProgrammingTaskExpectedAnswer],
        environment: ProgrammingEnvironment,
        executor: Executor,
    ):
        # TODO: figure out what to do with this variable
        expected_answer_by_step = {  # noqa: F841
            step_expected_answer.step_id: step_expected_answer.expected_answer
            for step_expected_answer in expected_answer
        }

        graph = Graph.model_validate({"steps": self.steps, "links": self.links})
        return await graph.run(user_input, environment, executor)


class TaskType(str, Enum):
    PROGRAMMING = "PROGRAMMING_TASK"


class ProgrammingTask(BaseModel):
    submission_id: str
    environment: ProgrammingEnvironment
    templates: list[File]
    testcases: list[Testcase]
    user_input: list[File]
    expected_answer: list[ProgrammingTaskExpectedAnswer]

    async def run(self, executor: Executor) -> TaskEvalResult[list[Any]]:
        expected_answer_by_testcase = {
            testcase_id: list(group)
            for testcase_id, group in groupby(self.expected_answer, lambda x: x.testcase_id)
        }

        results = []
        for testcase in self.testcases:
            testcase_expected_answer = expected_answer_by_testcase.get(testcase.id)
            if not testcase_expected_answer:
                print(f"WARN: Testcase {testcase.id} has no expected answer")
                continue
            results.append(
                await testcase.run(
                    self.user_input,
                    testcase_expected_answer,
                    self.environment,
                    executor,
                )
            )

        return TaskEvalResult(
            submission_id=self.submission_id,
            status=TaskEvalStatus.SUCCESS,
            result=results,
        )
