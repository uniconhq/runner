# runner

The part of Unicon that runs contestant code, and the contract files that
describe what it accepts and what it returns.

This repo produces two images and five contract files.

| Image | What it is |
|---|---|
| `ghcr.io/uniconhq/worker` | The supervisor that will set a grading machine up (feature 12). Today a placeholder that names the image and exits 0. |
| `ghcr.io/uniconhq/primitive-base` | The base the primitives build on: Python and the step wrapper, which runs a primitive as one Woodpecker step. `wrapper/`, `images/primitive-base/` |

Both build from `python:3.14-slim` pinned by digest.
The contract files are `plan.schema.json`, `result.schema.json`,
`primitive.schema.json`, `submission.schema.json` and `job.schema.json`,
published as assets on every release. The rest of the platform pins one
release and reads them from it. This repo holds no primitive: each primitive
is its own repo, `primitive-<name>`, built against the primitive contract
published here.

## The step wrapper

`wrapper/unicon_step.py`, standard library only, is `/usr/local/bin/unicon-step`
and the entrypoint of `primitive-base`. A primitive builds on that image and
names its own program as the image's command, `CMD ["/usr/local/bin/exec"]`,
so in a Woodpecker step the wrapper runs first and the primitive under it.
With no `data_url` setting it runs the command in its own place, so an image
started directly, as the primitives' own image tests start one, behaves as
before; in a Woodpecker step (`CI=woodpecker`), where the
forge gives every step of this image a link, it refuses with exit 2.

Given `data_url` and `data_token`, as `PLUGIN_DATA_URL` and
`PLUGIN_DATA_TOKEN`, it makes itself non-dumpable, so a process of its user
cannot read its `/proc/1/environ`, then:

1. asks the door for the step's job (`job.schema.json`), refusing another
   version with exit 2; a 204 says nothing is left for the step to do, as
   when another grading stored the same work first, and it exits 0 having
   done nothing, so Woodpecker still starts the later steps, while a 410,
   the grading finished, or a 401, the token ended, is exit 2;
2. fetches every file side by side, checking each against its size and
   SHA-256, or copies it from the workspace the clone step checked out,
   and makes each empty folder the job names;
3. writes `inputs.json` with each `{"secret": <setting>}` filled from the
   step's settings, refusing a setting of the wrapper's own and a value
   shorter than 8 characters, too short to mask, and each number exactly as
   the job wrote it;
4. runs the command in `/work` with every `PLUGIN_` variable taken out of its
   environment and `UNICON_BOX_USERS` and `UNICON_STEP_MEMORY_MB` set from
   the job, prints what it prints with each secret, the link and the token
   as `***`, a mask split across two reads of a long line included, and
   kills it and all it started at the job's time limit; once it ends, as the container's first process, it
   kills every process left, so nothing changes `out/` while it is uploaded;
5. masks `outputs.json` the same way, numbers kept exactly as written but
   one holding a secret, which becomes the text `***`, and an `outcome`
   never masked, or writes it itself, holding only `error`, when the step
   could not do its work or `outputs.json` is not a plain file; a primitive
   that wrote none is told with its exit code or the signal that killed it;
6. asks the door for an upload link per file, `outputs.json` and every file
   under `out/`, with its SHA-256 and size, puts each, and posts to done.
   Each file is opened once, without following a link, and hashed, measured
   and put from that one descriptor, its SHA-256 sent in base64 as
   `x-amz-checksum-sha256`, which its link is signed for, so the store
   takes those bytes or nothing.

It exits 0, or 1 on an error or, when the job says `fail_on_non_accepted`, an
outcome other than accepted or none at all, so Woodpecker starts no later
step. The token goes only to the door and links under it, the same scheme,
host and port and a path below the door's with no dot segment, escaped or
not, never to a presigned link. It follows no redirect and uses no proxy.
A request that finds no answer, a 429 or a 5xx, or an answer cut off midway
is made again, up to four times; a download from its start, though not a
write to the file that failed. An error names a file by its path, never
its link. Done ends the token, so a done or a report whose answer was cut
off and that, asked again, gets a 401 or 403 was taken. In a Woodpecker
step it refuses to start, exit 2, unless it is the container's first
process, as it must be to kill what a primitive leaves. The
pipeline's last step runs `primitive-base` with `mode: report` and only
posts `{}` to its own door, telling the forge the pipeline ended.

A step given the org's cache volume at `/cache`, a root step, as only exec
uses it, fetches a task file marked `cached` once per machine
into `/cache/objects/<sha256>`, under a lock per name, downloading into its
own folder on the volume, hashes an object already there again under its
lock, removing and fetching it anew unless it is still the length and
SHA-256 its name says, and hard-links it into a folder of its own under
`/cache/items/`, reached as `/work/cache`, held by a lock for the step's
life and removed at its end. A step given cached files and no volume is
refused. What killed steps left is swept: a folder whose lock no one holds,
with any part of a download in it, and, once an hour old, a lock still being
made or a folder without a lock, and the lock of every name no object has.
Past the cap, 10 GiB or the number of bytes in `/etc/unicon/cache-cap`, the
least recently used objects no running step links go; downloads in progress
are not counted. The volume takes the image's `/cache`, root's, mode 0755.
Its tests are in `wrapper/step_tests/`,
against a stand-in for the forge's door.

## The escape fixtures

`escape/escape.c` is one C program, built by the compile primitive as an
entrant's program would be, that tries every way out: memory, processes,
threads, CPU, sleeping, output and disk; the network, the cloud metadata
address and name lookup; writing outside its directory and reading other
runs' files, the task's answers and the sockets; `ptrace`, `mount`,
`unshare`, `bpf`, `keyctl`, loading a module and `setuid(0)`; its own
capabilities, NoNewPrivs, seccomp mode and uid; and any secret in reach.

It is the escape fixture of a run task, graded through a pipeline the forge
builds on a grading machine (`escape/`). `workflow.yaml` builds it as an
entrant's code and runs it in exec, one attack a run, the attack's name as
its input, and the task's check (`task/check/`) reads what it printed: a run
is accepted when no check escaped but those a box may pass
(`task/check/allowed`), and otherwise must end on the limit its attack is
bounded by (`expected.json`). Beside the attacks above it tries `container`:
the step container an exec box runs in, its devices, a cgroup's
`release_agent`, the step's files, `inputs.json` and cache, and its first
process. `escape/escape_tests/` checks that the task holds together;
deploy's end to end saves it on the dev stack and grades it
(`tests/e2e/test_escape.py`).

## The contracts

The plan, result, primitive and submission contracts share one
`schema_version`, 6, and a reader refuses a file written for another. A
release that changes the shape of any of them raises it. `job.schema.json` is
the wrapper's and has a version of its own, 1.
`scripts/check_contract_files.py` fails CI when a file does not accept its
reader's version, when an example under `examples/` does not validate, or
when a schema field is one no example sets.

**`plan.schema.json`.** Written by the `forge` repo at every valid save of a
task, as `plans/plan.json` in the task repo, and read by the forge, which
grades the task as Woodpecker pipelines. Nothing of the task's or the
workflow's is resolved at grade time. It holds the run workflow the task
names, pinned to a commit, and each workflow a box expanded; every task and
workflow file a step is given, by path, size and SHA-256; the entrant's
inputs; every run, with its id, its key and whether it is early; each
workflow step bound to the task's values and placed by how often it runs
(`save_once`, `save_per_run`, `grading_once`, `grading_per_run`); the
Woodpecker steps of the `save`, `early` and `late` pipelines, and the split
a grading takes while the task's results are released; the workflow's
reports; the save's notes; and the columns the entrant could set.

**`result.schema.json`.** Built by the forge once a grading's pipeline has
ended, from what each step's `outputs.json` held, and kept whole on the
grading's row: every run the pipeline grades, with its verdict, `passed` and
the reports its steps wrote, each text named by the stored output that holds
it; the reports and outcomes of the steps done once; what steps wrote for
staff alone; the number each value's program reported; a save's checks; and
`error`, a sentence for staff exactly when a run's verdict is `system_error`
or a value's program faulted. The outcomes are `accepted`, `wrong_answer`,
`time_limit`, `memory_limit`, `output_limit`, `runtime_error`,
`compile_error`, `skipped` and `system_error`; a step returns neither of the
last two.

**`primitive.schema.json`.** How a grading step and a primitive image talk,
and the `primitive.yaml` declaration the forge reads; a document is exactly
one of the three.

| File | Shape |
|---|---|
| `inputs.json` | `{"schema_version": 6, "inputs": {...}}`, or `"batch": [{"run": id, "inputs": {...}}]` |
| `outputs.json` | `{"schema_version": 6, "outputs": {...}}`, or `"batch": [{"run": id, "outputs": {...}}]` one per item in the same order, or `{"schema_version": 6, "error": "one sentence"}` when the primitive could not work at all |
| `primitive.yaml` | `schema_version: 6`, optional `title` and `description` for the workflow editor, `image` by digest, `batch`, `network`, `cache`, `limits` (all six), optional `limits_from` (`{input, scale, add}`, scale 1 and add 0 when left out) and `outputs_from`, `inputs` and `outputs` as `{type, options, optional, runs, secret, description}`, and on the `outcome` output an optional `outcomes`, each outcome the step can report (`accepted` to `compile_error`, never `skipped` or `system_error`) with one line saying when, for the workflow editor |

A batch item's `run` is an id of letters, digits, `_` and `-` that only the
forge knows the meaning of. A value is text, a number, a boolean, a file
`{"file": "in/..."}` or `{"file": "out/..."}`, or a folder `{"folder":
"in/..."}` or `{"folder": "out/..."}`, the tree under it with its layout.
Paths have no empty, `.` or `..` segment. Types are `text`, `number`,
`boolean`, `enum` with `options`, `file`, `folder`, and `outcome` for the one
output every primitive declares, and an input may also be one of the kinds
below. Every `file` or `folder` input says with `runs` whether the primitive
runs what arrives there as a program, and `secret` marks an input the image
keeps from any program it runs. The declaration names neither the primitive
nor its version: the repo at the forge is the name and its tag the version.
A primitive repo validates its `primitive.yaml` against the release's schema
with a placeholder image filled in, since bootstrap writes the image (below).
Beyond those:

- **A `named_files` input,** with `runs` as a file has, whose value is
  `{"files": {"<name>": {"file": "in/..."}}}`: files by the names the program
  sees them under. A name is letters, digits, `_`, `.` and `-`, not starting
  with a `.`. A port may fix the names with `names: [output, answer]`.
- **A `number` output may say `better`,** `higher` or `lower`, and its bounds
  `at_least` and `at_most`, both inclusive. `check_declaration.py` refuses
  bounds that cross or aren't finite, which a schema can't compare.
- **`outputs_from: manifest`** says the primitive also gives back the reports
  of the `program.yaml` on its `manifest` input, a file input it must declare.
- **A `programs` input,** named `programs`, takes programs the step names,
  one to eight, which exec's programs form runs side by side.
  The port declares `inputs`, what each program takes, as the primitive's
  own inputs are declared, `program` among them, a `file` with `runs:
  true`, and `outputs`, what it gives of each. The value
  is `{"programs": {"<name>": {<that program's inputs>}}}`; a name is a port
  name of at most 64 characters, never `wall_clock`. A declaration has at
  most one programs port, named `programs`, and needs `outputs_from:
  programs` with it, since the step's outputs come from that one port.
- **A `links` input** beside it takes the pipes between them, a list of
  `"<from> -> <to>"`, each at most once.
- **`outputs_from: programs`** says the primitive also gives back, for each
  program, `<name>.<output>` for each output the programs port declares,
  and on a verdict of `0` `<name>.<report>` for each report of the
  `program.yaml` on that program's `manifest` input, a file input the port
  must declare. An output name in `outputs.json` may so hold one `.`.
- **A limit may follow a sum or a largest** over the programs, `{sum:
  programs.memory_limit, add: 512}` or `{max: programs.time_limit, scale:
  4000, add: 6000}`, in place of `input`. `check_declaration.py` checks
  that the names are a programs port and a number input it declares, that a
  links port has a programs port beside it, and that a programs port is
  named `programs` and comes with `outputs_from: programs`.
- **`cache: true`** says the primitive reads the task's files through the
  machine's cache, so its step is given the cache; `false` when left out,
  and the step's files are copied in. `exec` says it;
  `compile` doesn't.
- **`forms`,** one to four ways to use the primitive, each with its own
  ports, limits and `outputs_from`, in place of those at the top. A step
  names no form: the save picks the one whose inputs the step gives, and
  `check_declaration.py` refuses a form with no required input of its own.

**`submission.schema.json`.** `submission.json` at the root of a submission
commit: `{"schema_version": 6, "inputs": {id: entry}}`, one entry per entrant
input the plan declares, where an entry is `{"files": ["files/<id>/<path>",
...]}` for a `file` input (one file) or a `folder` input (its files at their
paths in the folder), `{"value": ...}` for `text`, `number`, `boolean` and
`enum`, or, for an input inside the task's repeat, `{"parts": {"<part>":
entry}}`, one entry per part given, keyed by the position of its tuple from
1, its files under `files/<id>/<part>/`. A run whose part is not given is
skipped.

**`job.schema.json`.** What a grading step's door at the forge answers the
wrapper: `inputs`, the `inputs.json` to write, with only the runs still to
do in a batch; `files`, each placed at its path under `/work`, either a
download (`url`, `sha256`, `size`, and `cached` for a task file read
through the machine's cache), a copy of a `workspace` path, or an empty
`folder`; `uploads` and
`done`, the door's links for the upload links and the end; `time_limit` in
seconds; `box_users` and `step_memory_mb`, the memory the planner gave the
step, which the primitive caps itself at, for an exec step;
`fail_on_non_accepted`; and `secrets`, the settings that carry the step's
secrets. A 204 to the job's GET is nothing to do.

## The primitives' workflows

Every primitive repo, `primitive-<name>`, runs the same CI and the same
release, and both live here as reusable workflows. A primitive's own
`ci.yaml` and `release.yaml` only call them, at the runner release whose
primitive contract the primitive is built against, and pass that same tag as
`runner-ref`:

```yaml
jobs:
  ci:
    uses: uniconhq/runner/.github/workflows/primitive-ci.yaml@v0.6.0
    with:
      runner-ref: v0.6.0
```

A called workflow cannot tell which ref of its own repo it was called at, so
`runner-ref` says it. Both workflows check the primitive out as `primitive/`
and this repo at `runner-ref` as `runner/` beside it, so the primitive's tests
read the contract at `../runner/schemas/primitive.schema.json`, the file that
release publishes. Everything else is named after the calling repo: the image
is `ghcr.io/uniconhq/primitive-<name>`.

- `primitive-ci.yaml`, on a push to `main` and on a pull request: ruff, mypy,
  `scripts/check_declaration.py` on the primitive's `primitive.yaml`, and
  every primitive test except the image tests, in one job; in another, the
  image is built and the image tests (`pytest -m image`) run against it.
- `primitive-release.yaml`, on a tag push: the tag must be on `main`, be
  `v<major>.<minor>.<patch>` and equal the version in `pyproject.toml`. Its
  major is the primitive's version at the forge, which names the set of ports
  it declares: `v2.0.0` and `v2.1.3` are both `v2` there, so a release that
  changes a port raises the major. It runs the same checks as
  CI, builds the image once and runs the image tests on it, pushes exactly
  that image as `ghcr.io/uniconhq/primitive-<name>:v1.2.3`, and makes a
  GitHub release with `images.json`, naming the image by digest in the same
  shape as this repo's, and `primitive.yaml`, and the forge version in the
  notes. The caller grants
  `contents: write` and `packages: write`, and each job takes only its part.

`compile-image: true` is for a primitive that runs what the compile
primitive builds, such as exec and interact: both workflows build
`primitive-compile` from its `main` branch and hand it to the image tests as
`COMPILE_IMAGE`.

A primitive builds on `primitive-base`, named in its Dockerfile as `ARG
BASE_IMAGE`. CI builds that image from the `runner/` checkout and passes it,
also to the image tests as `BASE_IMAGE`; the release builds on
`ghcr.io/uniconhq/primitive-base:<runner-ref>`, which that runner release
pushed.

`scripts/check_declaration.py SCHEMA REPO` refuses an `image` line in the
repo's `primitive.yaml`, fills in a placeholder image and checks the result
against the schema's declaration. It reads the file as the forge reads a
definition file, YAML 1.2's core schema with every number that is not whole
an exact decimal (`scripts/core_yaml.py`), so an option `no` is the text
`no` here as at the forge; the workflows hand it `ruamel.yaml` with
`uv run --with`, so a primitive's own dependencies need not name it. Its
tests are in `scripts/declaration_tests/`.

A change to these workflows reaches a primitive when the primitive moves its
two `uses:` lines and `runner-ref` to the release that carries it.

## Rules that are cheap now and expensive later

**Every plan, job and primitive document carries `schema_version`, and a
mismatch is refused.** A writer that emits a shape its reader reads
differently produces a wrong result, not an error, and a wrong result is the
one failure nobody notices.

**No image is ever released as `:latest`.** A primitive's declaration names
its image by digest, so a task keeps grading the way it was saved, and a
grading machine pins the worker the same way. The base images are pinned by
digest too.

## Layout

```
wrapper/unicon_step.py           the step wrapper, primitive-base's entrypoint
wrapper/step_tests/              its tests, the stand-in for the forge's door, and
                                 the docker helpers its image tests use
escape/                          the escape fixtures' C program, and the run task
                                 and workflow that grade it through the forge
images/<name>/Dockerfile         the two images
images/placeholder.py            what the worker image runs
schemas/                         the five contract schemas, published on each release
examples/                        valid documents for every schema, checked in CI
scripts/                         the check that keeps the examples and schemas in step,
                                 and the declaration check every primitive runs
scripts/declaration_tests/       the declaration check's tests
.github/workflows/primitive-*    the CI and the release every primitive repo calls
```

## Running it locally

Python 3.14 with [uv](https://docs.astral.sh/uv/).

```
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run pytest -m docker
uv run python scripts/check_contract_files.py
```

`uv run pytest` runs the unit tests. `uv run pytest -m docker` needs a Docker
daemon: it builds `primitive-base` and runs the step wrapper in it against
its stand-in door: a primitive and a compile reading the wrapper's environment get nothing,
nothing a primitive leaves running outlives it, the org's cache volume serves
the root steps that use it, and exec built on the base image from a
`primitive-exec` checkout beside this one runs a batch on isolate through the
wrapper, a cached file bound into its box (the compile and exec cases are
skipped without `primitive-compile` and `primitive-exec` checkouts there).

CI runs those commands, checks that the Dockerfiles on the python base share
one digest, builds the two images, and starts each once: the worker to see
its placeholder, and primitive-base to see the wrapper run its command
without a data link.

```
docker build -f images/worker/Dockerfile -t unicon-worker:dev .
docker build -f images/primitive-base/Dockerfile -t unicon-primitive-base:dev .
```

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks the tag against the version in `pyproject.toml`, runs
the same checks as CI, pushes the two images as `ghcr.io/uniconhq/worker:v1.2.3`
and `primitive-base:v1.2.3`, and creates a GitHub release with the five
contract files and `images.json` attached, which names each image by digest,
and the same two digests in the notes. `deploy` pins that release and those
digests.

The first push creates each package on the organisation as **private**,
whatever the repo's visibility is. Grading machines pull them anonymously, so
someone has to open each package on the organisation's Packages page once and
set its visibility to public, and add the `runner` repo under
Manage Actions access so later releases can keep pushing to it. Until that is
done the first `docker pull` from a machine fails with a 403 that reads like a
missing tag.
