"""The one exception that ends a grading as a system error."""


class GradingError(Exception):
    """Something the harness did not expect: a plan it cannot run, a primitive's
    error, an outputs.json that breaks the contract, a container refused or
    killed for a reason other than a per-test limit. It never becomes a grade;
    it stops the run as a system_error whose error is `message`, written for
    staff.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
