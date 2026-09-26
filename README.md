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
contract and stops. It does not download bundles, run a plan, start a sandbox
or write a verdict. The other three images carry a placeholder entrypoint that
names the image and exits 0; their programs come with the features that need
them. This repo holds no primitive: each primitive is its own repo,
`primitive-<name>`, built against the primitive contract published here.

## How a grading job will work

Woodpecker starts the harness image once per submission with two values in its
environment: `UNICON_JUDGING_ID` and `UNICON_ENVELOPE_URL`, a URL to a small
file the backend wrote to object storage. The harness fetches the envelope and
learns everything else from it: presigned URLs for the task bundle, the
submission bundle and the compiled plan, presigned URLs to write the verdict
and the log back to, a callback address with a one-job token, and a deadline.
The callback address is under `/api/v1/`, the prefix the proxy sends to the
backend, so an agent outside the compose network reaches it the same way a
browser does.

It then reads the plan, runs the steps in order, starts a sandbox for any step
that runs contestant code, posts progress as tests finish, writes
`verdict.json`, and exits zero only once the backend has accepted the result.

## The four contracts

**`schemas/envelope.schema.json`.** Written by the backend when it dispatches a
job; read by the harness at startup. It is the only thing the harness is told,
so everything a job needs is in it and nothing that outlives the job is.

| Field | What it carries |
|---|---|
| `schema_version` | `1`. A version the harness does not speak is refused, not guessed at |
| `judging_id` | The judgings row. Must equal `UNICON_JUDGING_ID` |
| `submission` | `org`, `repo`, `tag`, `commit`: the Forgejo identity of what is graded |
| `stage`, `attempt` | The contest's stage name, and 1 or higher for a rejudge |
| `task` | `org`, `repo`: the Forgejo identity of the task graded against |
| `task_published_tag`, `task_published_sha` | The published tag of that task repo, and the commit it pointed at |
| `urls` | Presigned: `task_bundle`, `submission_bundle`, `plan` to read; `result_put`, `log_put` to write |
| `callback` | `base_url`, the backend's `/api/v1/internal/judgings/<judging_id>`, and a `token` good for this job only |
| `digests` | Optional. sha256 of the three objects downloaded, so bytes can be checked before they are trusted |
| `deadline` | After this instant the token is dead and the backend treats the job as lost |
| `limits` | Optional. `wall_seconds` for the whole job; per-step limits live in the plan |

The task fields are there because the harness copies them into the verdict; it
does not otherwise use them.

**`schemas/plan.schema.json`.** Written by the backend's compiler when a task is
published; read by the harness at grade time. A plan is a flat list of
primitive calls with every workflow reference resolved, pinned to one grading
image by digest. The harness never reads workflow YAML.

| Field | What it carries |
|---|---|
| `schema_version` | `1` |
| `image_digest` | The harness image this plan was compiled against, `sha256:...`, never a tag |
| `stage` | The stage this plan belongs to; a task may compile one plan per stage |
| `steps[].id` | Unique in the plan; how later steps name this one |
| `steps[].primitive` | `owner/name@version`, a primitive the forge holds at that version |
| `steps[].inputs` | Arguments, keyed by the input names the primitive declares. Open: the value grammar is settled with the compiler |
| `steps[].for_each` | Optional. Run the step once per item of the named list and collect the outputs |
| `steps[].limits` | Optional. `time_ms`, `cpu_ms`, `memory_mb`, `pids`, `output_mb` for one run |

**`schemas/verdict.schema.json`.** Written by the harness at the end of a job;
read by the backend when the result callback arrives, and read again by
reconciliation if the Unicon database is ever lost. That second reader is why
the file repeats the Forgejo identity of both sides: Forgejo survives what
Postgres does not, so a verdict that names its own `submission` and `task` can
be matched back without rejudging everything. `DATA-MAP.md` puts the link from
a judging to its task in the Unicon database, which is exactly the link this
file has to carry on its own.

| Field | What it carries |
|---|---|
| `schema_version` | `1` |
| `judging_id`, `submission`, `stage`, `attempt` | Copied from the envelope |
| `task`, `task_published_tag` | The task repo and the published tag this was graded against |
| `outcome` | `verdict`, `contestant_error` or `system_error`: who owns the failure |
| `verdict` | The workflow's own word, for example `AC`. Null unless the outcome is `verdict` |
| `score` | Text, not a number: `-?digits(.digits)?`. Null unless the outcome is `verdict` |
| `metrics` | Optional measurements; keys and types belong to the workflow |
| `summary` | The per-test table: `id`, `verdict`, `time_ms`, `memory_kb`, optional `message` |
| `started_at`, `finished_at` | When the harness accepted the envelope and finished the last step |
| `error_message` | One sentence for a human when the outcome is not `verdict`; null when it is |

The outcome decides the shape, and the schema enforces it: a `verdict` outcome
carries a verdict, a score and a summary and no error message; the other two
carry an error message and no verdict or score. A harness that crashed and
still filed a `WA` against a contestant is the failure that rule prevents.

`score` is a string because JSON numbers are binary floats: a decimal score
would not survive the round trip byte for byte, and a leaderboard that
disagrees with the number the contestant was shown is a protest. The grammar is
narrow on purpose, since the backend parses it once into the numeric column the
leaderboard sorts on, and a value it cannot parse arrives long after the
grading, when nobody can fix it.

**`schemas/primitive.schema.json`.** How the harness and a primitive image
talk. The harness starts one sandboxed container from the step's image and
mounts one working directory into it. `inputs.json` and the input files under
`in/` go in; `outputs.json` and the output files under `out/` come back when
the container exits. The schema describes both files, and a primitive reads
one directory and writes one directory and sees nothing else: not the forge,
not the plan, not the network.

| File | Field | What it carries |
|---|---|---|
| `inputs.json` | `schema_version` | `1` |
| | `step` | The plan step this container runs, for the primitive's own log |
| | `inputs` | One value per declared input, keyed by name |
| `outputs.json` | `schema_version` | `1` |
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

**The harness runs on machines we do not own.** It never talks to Forgejo, and
the only credential it ever holds is the callback token for the one job it is
running, which dies at the deadline in the envelope. Anything that needs a
long-lived secret belongs in the backend, not here.

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
`stack-test-findings.md` section 1.1. Not implemented yet.

**Every sandbox carries a label with the judging id.** If the harness dies
mid-job its sandbox containers keep running and nothing cleans them up. The
harness kills its own in a `finally` block, and a reaper on the machine removes
any `unicon.judging=*` container older than the maximum job time, for the case
where the harness is not there to do it. `stack-test-findings.md` section 1.2.
Not implemented yet.

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
  -e UNICON_JUDGING_ID=0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31 \
  unicon-harness:dev
```

It exits 0 and prints one line for an envelope it accepts. For anything else it
exits 2 and prints one line to stderr that starts with
`unicon-harness: <code>:`, where the code is one of:

| Code | What happened |
|---|---|
| `missing_environment` | `UNICON_ENVELOPE_URL` or `UNICON_JUDGING_ID` is not set |
| `envelope_unreachable` | The envelope URL did not answer, answered an error, or is not a URL |
| `envelope_not_json` | What came back is not a JSON object |
| `schema_version_mismatch` | The envelope is written against a version this image does not speak |
| `schema_violation` | The envelope does not match `envelope.schema.json` |
| `judging_id_mismatch` | The envelope is for a different judging than the environment says |
| `schemas_missing` | The image was built without the contract files. A packaging fault, not a job fault |

Exit 2 means the job never began, and is reported to the backend as a
`system_error` once the harness runs plans. There is one exit code for every refusal because the backend's
only decision is whether to alert; the code in the line is for the person
reading the log.

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
