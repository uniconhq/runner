# runner

The part of Unicon that runs contestant code, and the contract files that
describe what it accepts and what it returns.

This repo produces the grading image, `ghcr.io/uniconhq/grading`, and four
files published as assets on every release: `plan.schema.json`,
`envelope.schema.json`, `verdict.schema.json` and `registry.json`. The backend
pins one release and generates its models from those files.

Today the image contains a harness that reads the envelope it is handed,
checks it against the contract and stops. It does not download bundles, run a
plan, start a sandbox or write a verdict. Those are Task 6. `unicon-worker`,
the image a bring-your-own-compute operator installs, is Task 12.

## How a grading job will work

Woodpecker starts the grading image once per submission with two values in its
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

## The three contracts

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
| `image_digest` | The grading image this plan was compiled against, `sha256:...`, never a tag |
| `stage` | The stage this plan belongs to; a task may compile one plan per stage |
| `steps[].id` | Unique in the plan; how later steps name this one |
| `steps[].primitive` | `owner/name@version`, listed in the `registry.json` of the pinned release |
| `steps[].inputs` | Arguments, keyed by the input names in the registry. Open: the value grammar is Task 10's |
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

`registry.json` is the fourth file. It lists every primitive the grading image
offers, and the compiler validates a workflow against it at publish, so a typo
or a wrong type is the setter's error at the click rather than a green pipeline
that marks everyone wrong. It is empty today; Task 10 fills it. The entry shape:

```json
{
  "schema_version": 1,
  "primitives": [
    {
      "name": "unicon/diff-check@v1",
      "description": "Compare a run's output with the expected answer.",
      "inputs": [
        { "id": "actual", "type": "file", "required": true },
        { "id": "expected", "type": "file", "required": true }
      ],
      "outputs": [
        { "id": "verdict", "type": "text" },
        { "id": "score", "type": "number" }
      ]
    }
  ]
}
```

Types are the task input types in `TASK-FORMAT.md`, section 2.

Every schema field is set by the matching file in `examples/`, and
`scripts/check_contract_files.py` fails if one is not. A field no example fills
in is a field nothing writes, and it stays plausible for years because the
schema still describes it.

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
`stack-test-findings.md` section 1.1. Not implemented yet; Task 6 builds it.

**Every sandbox carries a label with the judging id.** If the harness dies
mid-job its sandbox containers keep running and nothing cleans them up. The
harness kills its own in a `finally` block, and a reaper on the machine removes
any `unicon.judging=*` container older than the maximum job time, for the case
where the harness is not there to do it. `stack-test-findings.md` section 1.2.
Task 6 and Task 12.

**The image is never released as `:latest`.** Plans pin the grading image by
content digest so a task keeps grading the way it was published. A moving tag
invites someone to run a plan against an image it was not compiled for. The
base image is pinned by digest for the same reason; bumping it is a deliberate
commit.

## Layout

```
harness/unicon_harness/   the program in the image
harness/tests/            its tests
schemas/                  the three contract schemas, published on each release
registry.json             the primitive registry, published on each release
examples/                 one valid plan, envelope and verdict, checked in CI
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

CI runs exactly those six commands, then builds the image and starts it once
with an empty environment to see that the entrypoint refuses it.

To run the harness against a real envelope over HTTP, serve one and point the
image at it:

```
python -m http.server 8799 --directory examples
docker build -t unicon-grading:dev .
docker run --rm --add-host=host.docker.internal:host-gateway \
  -e UNICON_ENVELOPE_URL=http://host.docker.internal:8799/envelope.json \
  -e UNICON_JUDGING_ID=0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31 \
  unicon-grading:dev
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

Exit 2 means the job never began; Task 6 reports that to the backend as a
`system_error`. There is one exit code for every refusal because the backend's
only decision is whether to alert; the code in the line is for the person
reading the log.

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks the tag against the version in `pyproject.toml`, runs
the same checks as CI, pushes `ghcr.io/uniconhq/grading:v1.2.3`, and creates a
GitHub release with the four contract files attached and the image digest in
the notes. The backend pins that release and that digest.

The first push creates the `grading` package on the organisation as **private**,
whatever the repo's visibility is. Grading agents pull it anonymously, so
someone has to open the package on the organisation's Packages page once and
set its visibility to public, and add the `runner` repo under Manage Actions
access so later releases can keep pushing to it. Until that is done the first
`docker pull` from an agent fails with a 403 that reads like a missing tag.
