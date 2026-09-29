# runner

The part of Unicon that runs contestant code, and the contract files that
describe what it accepts and what it returns.

This repo produces four images and four contract files. The images are
`ghcr.io/uniconhq/harness`, the program that runs a plan by starting one
sandboxed container per step; `ghcr.io/uniconhq/worker`, the supervisor that
sets a grading machine up; `ghcr.io/uniconhq/clone`, what the CI checks code
out with; and `ghcr.io/uniconhq/socket-filter`, the policy that stands between
the harness and Docker. All four build from `python:3.14-slim` pinned by
digest, under `images/`. The contract files are `plan.schema.json`,
`envelope.schema.json`, `verdict.schema.json` and `primitive.schema.json`,
published as assets on every release. The rest of the platform pins one
release and reads them from it.

Today the harness reads the envelope it is handed, checks it against the
contract and stops. It does not run a plan, start a sandbox or post a verdict.
The other three images carry a placeholder entrypoint that names the image and
exits 0; their programs come with the features that need them. This repo holds
no primitive: each primitive is its own repo, `primitive-<name>`, built against
the primitive contract published here.

## How a grading run will work

The `forge` repo starts a grading run at the CI as the org's own account and
tells the CI the same three steps for every run: check out the publication
with its large files, check out the submission, run the harness image by the
digest in the plan. The harness starts with two values in its environment:
`UNICON_GRADING_ID` and `UNICON_ENVELOPE_URL`, a URL to a small file the
`forge` repo wrote. The harness fetches the envelope and learns the rest from
it: which run this is, where the two checkouts are, a callback address with a
one-run token, a presigned URL to write the log to, and a deadline. Nothing
else is downloaded: the plan and the task files are in the publication
checkout, the contestant's files in the submission checkout. The callback
address is under `/api/`, the prefix the proxy sends to the backend, so a
machine outside the compose network reaches it the same way a browser does.

It then reads `plans/<stage>.json` from the publication checkout, runs the
steps in order, one sandboxed container per step from the step's image by
digest, posts progress as tests finish, writes the log by the presigned URL,
posts the verdict through the callback, and exits zero only once the `forge`
repo has accepted it.

## The four contracts

The four share one `schema_version`, the one a runner release publishes them
at. A release that changes the shape of any of them raises it, so a harness
refuses a file written for a shape it does not read rather than reading it
wrongly; `scripts/check_contract_files.py` fails CI when a file pins another.

**`schemas/envelope.schema.json`.** Written by the `forge` repo when it
dispatches a run; read by the harness at startup. It is the only thing the
harness is told, so everything a run needs is in it and nothing that outlives
the run is.

| Field | What it carries |
|---|---|
| `schema_version` | `2`. A version the harness does not speak is refused, not guessed at |
| `grading_id` | The gradings row. Must equal `UNICON_GRADING_ID` |
| `submission` | `org`, `repo`, `tag`, `commit`: the forge identity of what is graded |
| `stage`, `attempt` | The contest's stage name, and 1 or higher for a rejudge. The plan is `plans/<stage>.json` in the publication checkout |
| `task` | `org`, `repo`: the forge identity of the task graded against |
| `publication` | `tag`, `commit`: the publication of that task, and the commit it pointed at, which is what was checked out |
| `checkouts` | `task`, `submission`: absolute paths inside the harness container where the CI put the two checkouts |
| `callback` | `url`, the `forge` repo's address for this run, and a `token` good for this run only |
| `log_put` | Presigned PUT for the run log, the one thing the harness writes to object storage |
| `deadline` | After this instant the token is dead and the run is treated as lost |
| `limits` | Optional. `wall_seconds` for the whole run; per-step limits live in the plan |

The task and publication fields are there because the harness copies them into
the verdict; it does not otherwise use them.

**`schemas/plan.schema.json`.** Written by the `forge` repo's compiler at every
valid save of a task, into the task repo as `plans/<stage>.json`, in the
commit its publication points at; read by the harness at grade time. A plan
is a flat list of primitive calls with every workflow reference resolved and
every list expanded, and every step carries the image it runs from by digest,
its entrypoint and all its limits, so nothing is resolved at grade time and
the harness never talks to the forge or reads workflow YAML.

| Field | What it carries |
|---|---|
| `schema_version` | `2` |
| `harness_image` | The harness image this plan was compiled for, as a full reference by digest, never a tag |
| `stage` | The stage this plan belongs to; a task compiles one plan per stage |
| `lists` | The lists a `for_each` names, already expanded from the task's files, for example the test list |
| `steps[].id` | Unique in the plan; how later steps name this one |
| `steps[].primitive` | `name@version`, for the log and the per-test table; never resolved at grade time |
| `steps[].image`, `steps[].entrypoint` | The primitive's image at that version by digest, and the program the container runs |
| `steps[].inputs` | Arguments, keyed by the input names the primitive declares: a literal, a path into a checkout, `steps.<id>.<output>` or `item.<field>` |
| `steps[].for_each` | Optional. Run the step once per item of the named list and collect the outputs |
| `steps[].limits` | `time_ms`, `cpu_ms`, `memory_mb`, `pids`, `output_mb` for one run, all of them, so the socket filter can refuse a container carrying less |

**`schemas/verdict.schema.json`.** Posted by the harness through the callback
at the end of a run; checked by the `forge` repo against this schema and kept
on the gradings row, and read again by reconciliation if the Unicon database
is ever lost. That second reader is why the verdict repeats the forge identity
of both sides: the forge survives what Postgres does not, so a verdict that
names its own `submission`, `task` and `publication` can be matched back
without rejudging everything.

| Field | What it carries |
|---|---|
| `schema_version` | `2` |
| `grading_id`, `submission`, `stage`, `attempt`, `task`, `publication` | Copied from the envelope |
| `outcome` | One of `accepted`, `partial`, `wrong_answer`, `time_limit`, `memory_limit`, `output_limit`, `runtime_error`, `compile_error`, `skipped`, `system_error` |
| `metrics` | Named numbers over the whole run, for example `points` or `accuracy`. A leaderboard ranks on one of these by name |
| `tests` | One row per test: `id`, its own `outcome`, `time_ms`, `memory_kb`, `metrics`, optional `message` |
| `summary` | A few lines for the contestant, as the task's visibility allows |
| `resources` | `wall_ms`, `cpu_ms`, `peak_memory_kb` over the whole run |
| `log` | The object URL the log went to, without the presigned query, or null |
| `started_at`, `finished_at` | When the harness accepted the envelope and finished the last step |

There is no separate score: whatever number a task is ranked on is a metric
the leaderboard names, so a scorer that emits `points` and one that emits
`accuracy` are the same shape, and the `forge` repo needs to understand only
the outcome list and the per-test rows. `system_error` means nobody graded:
the schema then allows no test rows and no metrics, and the summary says what
went wrong for staff. A harness that crashed and still filed a wrong answer
against a contestant is the failure that rule prevents.

**`schemas/primitive.schema.json`.** How the harness and a primitive image
talk. The harness starts one sandboxed container from the step's image and
mounts one working directory into it. `inputs.json` and the input files under
`in/` go in; `outputs.json` and the output files under `out/` come back when
the container exits. The schema describes both files, and a primitive reads
one directory and writes one directory and sees nothing else: not the forge,
not the plan, not the network.

| File | Field | What it carries |
|---|---|---|
| `inputs.json` | `schema_version` | `2` |
| | `step` | The plan step this container runs, for the primitive's own log |
| | `inputs` | One value per declared input, keyed by name |
| `outputs.json` | `schema_version` | `2` |
| | `outputs` | One value per declared output, keyed by name |
| | `error` | Optional. One sentence when the primitive could not do its work at all; a failed compile is an outcome, not an error |

A value is text, a number or a boolean carried as itself, a file as
`{"file": "in/main.cpp"}`, or a list of files as a list of those objects.
File paths are relative to the working directory, under `in/` for inputs and
`out/` for outputs. There is no registry file: the forge holds every
primitive and its declaration, and the compiler reads them from there.

Every schema field is set by a file in `examples/`, and
`scripts/check_contract_files.py` fails if one is not. A field no example fills
in is a field nothing writes, and it stays plausible for years because the
schema still describes it. The primitive contract has three examples, one per
file it describes and one for the error case.

## Rules that are cheap now and expensive later

**The harness runs on machines we do not own.** It never talks to the forge,
and the only credential it ever holds is the callback token for the one run it
is running, which dies at the deadline in the envelope. Anything that needs a
long-lived secret belongs in the `forge` repo, not here.

**Every plan and envelope carries `schema_version`, and a mismatch is refused.**
A compiler that emits a shape the harness reads differently produces a wrong
verdict, not an error, and a wrong verdict is the one failure nobody notices.
The harness stops with a `system_error` instead of grading against a shape it
does not know.

**Sandbox paths are host paths, not the harness's own paths.** The Docker
daemon resolves bind-mount sources on the host. A harness that mounts its own
`/work/sandbox` into a sibling container gets an empty directory and a silently
wrong verdict. It must ask the daemon for its own mounts once at startup and
translate every sandbox path through that. Measured and confirmed in
Not implemented yet.

**Every sandbox carries a label with the grading id.** If the harness dies
mid-run its sandbox containers keep running and nothing cleans them up. The
harness kills its own in a `finally` block, and a reaper on the machine removes
any `unicon.grading=*` container older than the maximum run time, for the case
where the harness is not there to do it. Not implemented yet.

**No image is ever released as `:latest`.** Plans pin the harness image by
content digest so a task keeps grading the way it was published, and a grading
machine pins the other three the same way. A moving tag invites someone to run
a plan against an image it was not compiled for. The base image is pinned by
digest for the same reason; bumping it is one commit that changes all four
Dockerfiles.

## Layout

```
images/harness/           the harness Dockerfile; the program is harness/
images/worker/            the worker Dockerfile, placeholder entrypoint
images/clone/             the clone Dockerfile, placeholder entrypoint
images/socket-filter/     the socket filter Dockerfile, placeholder entrypoint
images/placeholder.py     what the three placeholder images run
harness/unicon_harness/   the program in the harness image
harness/tests/            its tests
schemas/                  the four contract schemas, published on each release
examples/                 valid documents for every schema, checked in CI
scripts/                  the check that keeps the examples and schemas in step
```

## Running it locally

Python 3.14 with [uv](https://docs.astral.sh/uv/).

```
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run python scripts/check_contract_files.py
```

CI runs exactly those six commands, then builds the four images from the repo
root, starts the harness once with an empty environment to see that it refuses
it, and starts each of the other three to see that its placeholder runs.

```
docker build -f images/harness/Dockerfile -t unicon-harness:dev .
docker build -f images/worker/Dockerfile -t unicon-worker:dev .
docker build -f images/clone/Dockerfile -t unicon-clone:dev .
docker build -f images/socket-filter/Dockerfile -t unicon-socket-filter:dev .
```

To run the harness against a real envelope over HTTP, serve one and point the
image at it:

```
python -m http.server 8799 --directory examples
docker run --rm --add-host=host.docker.internal:host-gateway \
  -e UNICON_ENVELOPE_URL=http://host.docker.internal:8799/envelope.json \
  -e UNICON_GRADING_ID=0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31 \
  unicon-harness:dev
```

It exits 0 and prints one line for an envelope it accepts. For anything else it
exits 2 and prints one line to stderr that starts with
`unicon-harness: <code>:`, where the code is one of:

| Code | What happened |
|---|---|
| `missing_environment` | `UNICON_ENVELOPE_URL` or `UNICON_GRADING_ID` is not set |
| `envelope_unreachable` | The envelope URL did not answer, answered an error, or is not a URL |
| `envelope_not_json` | What came back is not a JSON object |
| `schema_version_mismatch` | The envelope is written against a version this image does not speak |
| `schema_violation` | The envelope does not match `envelope.schema.json` |
| `grading_id_mismatch` | The envelope is for a different grading run than the environment says |
| `schemas_missing` | The image was built without the contract files. A packaging fault, not a job fault |

Exit 2 means the run never began, and is reported to the `forge` repo as a
`system_error` verdict once the harness runs plans. There is one exit code for
every refusal because the `forge` repo's only decision is whether to alert;
the code in the line is for the person reading the log.

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks the tag against the version in `pyproject.toml`, runs
the same checks as CI, pushes the four images as `ghcr.io/uniconhq/harness:v1.2.3`,
`worker:v1.2.3`, `clone:v1.2.3` and `socket-filter:v1.2.3`, and creates a
GitHub release with the four contract files and `images.json` attached, which
names each image by digest, and the same four digests in the notes. `deploy`
pins that release and those digests.

The first push creates each package on the organisation as **private**,
whatever the repo's visibility is. Grading machines pull them anonymously, so
someone has to open each of the four packages on the organisation's Packages
page once and set its visibility to public, and add the `runner` repo under
Manage Actions access so later releases can keep pushing to it. Until that is
done the first `docker pull` from a machine fails with a 403 that reads like a
missing tag.
