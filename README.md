# runner

The base every Unicon grading primitive runs on, and the contract files that
describe gradings.

It holds:

- `wrapper/unicon_step.py`: the step wrapper. Each grading step runs it
  first. It fetches the step's job from the forge, runs the primitive, masks
  secrets in its output, and uploads the results.
- `schemas/`: JSON schemas for the plan, job, result, primitive and
  submission files. `examples/` has a valid example for each.
- `images/`: two images. `primitive-base` is Python plus the wrapper.
  `worker` will set up grading machines; today it is a placeholder.
- `escape/`: a C program that tries every way out of the sandbox, and a task
  that grades it.
- `.github/workflows/primitive-*.yaml`: the CI and release workflows that
  every `primitive-<name>` repo calls.

Primitives such as `primitive-exec` build on `primitive-base`. The forge
writes and reads the schemas. `deploy` pins a runner release and its image
digests.

## Run it locally

You need Python 3.14 and [uv](https://docs.astral.sh/uv/).

```
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run python scripts/check_contract_files.py
```

The Docker tests need a running Docker daemon. They build `primitive-base`
and run the wrapper in it. The exec cases need a `primitive-exec` checkout
next to this repo; without it they are skipped.

```
uv run pytest -m docker
```

To build the images by hand:

```
docker build -f images/worker/Dockerfile -t unicon-worker:dev .
docker build -f images/primitive-base/Dockerfile -t unicon-primitive-base:dev .
```

CI (`.github/workflows/ci.yaml`) runs all of the above, plus zizmor and
actionlint on the workflows.

## Release

1. Set `version` in `pyproject.toml` and merge to `main`.
2. Push a tag `v<version>`, for example `v1.2.3`, on a `main` commit.

`release.yaml` checks that the tag is on `main` and matches `pyproject.toml`.
It reruns all checks, including the Docker tests. Then it pushes
`ghcr.io/uniconhq/worker:v1.2.3` and `ghcr.io/uniconhq/primitive-base:v1.2.3`.
It makes a GitHub release with the five schemas and `images.json`, which
names each image by digest.

The first push of a new image makes its package private. Grading machines
pull without logging in, so set each package to public once on the org's
Packages page. Also give the `runner` repo Actions access there. Until then,
pulls fail with a 403.

A primitive repo calls the shared workflows at a runner tag and passes the
same tag as `runner-ref`:

```yaml
jobs:
  ci:
    uses: uniconhq/runner/.github/workflows/primitive-ci.yaml@v0.13.0
    with:
      runner-ref: v0.13.0
```

A change to those workflows reaches a primitive only when it moves to the new
tag.

## Don't break

- The plan, result, primitive and submission schemas share one
  `schema_version` (now 6). `job.schema.json` has its own (now 2). Raise it
  when a shape changes. Readers refuse other versions.
- `check_contract_files.py` fails when an example does not validate, or a
  schema field has no example. Update `examples/` with the schemas.
- Never publish an image as `:latest`. Images are pinned by digest.
- Both Dockerfiles use the same `python:3.14-slim` digest. Change them
  together; CI checks this.
