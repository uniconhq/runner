"""Where the contract files live and which version of them this harness speaks.
The plan, envelope, verdict and primitive schemas ship as release assets beside
the four images, and the rest of the platform pins one runner release.
"""

from functools import cache
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA_CANDIDATES = (
    Path(__file__).parent / "schemas",
    Path(__file__).parents[2] / "schemas",
)


class SchemasMissingError(Exception):
    """The image was built without the contract files. A packaging mistake rather
    than a job failure, so it names the directories it looked in.
    """


@cache
def schema_dir() -> Path:
    """The directory holding the four contract schemas. Resolved once: it cannot
    change while the process runs.
    """
    for candidate in SCHEMA_CANDIDATES:
        if candidate.is_dir():
            return candidate
    looked_in = " or ".join(str(candidate) for candidate in SCHEMA_CANDIDATES)
    raise SchemasMissingError(
        f"no contract schemas in this image: looked in {looked_in}"
    )
