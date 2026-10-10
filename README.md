# runner

The part of Unicon that runs contestant code, and the contract files that
describe what it accepts and what it returns.

This repo produces four images and five contract files.

| Image | What it is |
|---|---|
| `ghcr.io/uniconhq/harness` | The program the CI runs once per grading run. It runs the task's plan, one sandboxed container per step, and reports the result. `harness/` |
| `ghcr.io/uniconhq/socket-filter` | The policy between the harness and a machine's Docker. It holds the daemon's socket and gives the harness its own. `socket_filter/` |
| `ghcr.io/uniconhq/clone` | What the CI's two checkout steps run: the CI's clone plugin with big files kept in `/lfs-cache`. `images/clone/` |
| `ghcr.io/uniconhq/worker` | The supervisor that will set a grading machine up (feature 12). Today a placeholder that names the image and exits 0. |

The harness, socket filter and worker build from `python:3.14-slim` pinned by
digest; the clone image builds from `woodpeckerci/plugin-git` pinned by digest.
The contract files are `plan.schema.json`, `envelope.schema.json`,
`result.schema.json`, `primitive.schema.json` and `submission.schema.json`,
published as assets on every release. The rest of the platform pins one release
and reads them from it. This repo holds no primitive: each primitive is its own
repo, `primitive-<name>`, built against the primitive contract published here.

## How a grading run works

The `forge` repo starts a grading run at the CI. The CI clones the task at its
publication into `/woodpecker/task` (big files included) and the submission
into `/woodpecker/submission`, both with the clone image, then runs the harness
image by the digest in the plan, with the socket filter's socket directory
mounted read-only at `/run/unicon` and
`DOCKER_HOST=unix:///run/unicon/docker.sock`. The harness
holds no credential but the run's callback token and no Docker socket but the
filter's.

In order, the harness:

1. Fetches the envelope from `UNICON_ENVELOPE_URL` and refuses it unless its
   `schema_version`, its shape and its grading id (`UNICON_GRADING_ID`) are
   right. A refusal exits 2 and reports nothing, because an envelope it does
   not trust gives it no callback it can trust.
2. Posts `{"event": "started"}` to the callback. A 401, 403, 404, 409 or 410
   there means the forge will not take the run, and it stops.
3. Asks the daemon, through the filter, how its own container is mounted, and
   finds the volume the checkouts are in (the host-path trap, below). It makes
   `unicon-steps/` in that volume, gives it to uid 10001 and becomes uid 10001
   for the rest of the run. The image starts as root only for this, because
   the CI's workspace belongs to root.
4. Reads `plans/plan.json` from the task checkout and `submission.json` and
   the contestant's files from the submission checkout, and checks the one
   against the other. Nothing else is downloaded.
5. Runs the plan (below), posting progress after each container.
6. Checks the result against `result.schema.json`, puts the run log by
   the presigned `log_put` URL and posts `{"event": "finished", "result":
   ...}`, retrying until the envelope's deadline. Every number in it is
   written exactly as the primitive wrote it.

It exits 0 once the forge accepted the result, 1 when the result could not be
delivered, and 2 when the run never began. Anything unexpected after the
envelope is accepted stops the run as a `system_error`, with a sentence for
staff in `error`, never a grade.

### Running a plan

- A test with no file in a per-test contestant input is `skipped` before any
  step runs, and left out of every per-test step.
- Steps run in plan order, one container each, every step that runs once
  first. A step's inputs are copied into its own directory under `in/`, each
  distinct one once: a file at `in/<n>/<its name>`, a folder (a task path
  ending in `/`, a folder contestant input, a folder output of an earlier
  step) as one directory `in/<n>/<its name>/` with its layout, and a file
  given to a port the plan's `folders` names as a folder holding that one
  file, `in/<n>/<port>/<its name>`. Nothing a step wrote reaches another step
  except through the plan's references. A port wired to an optional output
  (a `?` in the plan's `outputs`) that its step did not write is left out of
  `inputs.json`.
- Values the harness fills in: a contestant input from `submission.json`
  (inside an entry for one test, a per-test input is that test's file), a
  secret from the envelope, written into `inputs.json` as text, and a template
  with the contestant's scalars written in: a number as the shortest decimal
  with no exponent (`2.50` is `2.5`, `2.0` is `2`), a boolean as `true` or
  `false`, text and an enum as they are, and `{{` and `}}` as braces.
- A step that runs once and returns an `outcome` other than `accepted` stops
  the run: later steps are skipped, every test is `skipped`, the result's
  `stopped` is that outcome (a failed compile is `compile_error`) and its
  `stopped_by` the step's id.
- A per-test step runs only for the tests with no outcome yet. One returning
  another outcome for a test gives the test that outcome and skips its later
  steps (a batch leaves it out).
- What a step wrote is checked before anything reads it: `outputs.json`
  against the primitive contract, read with every number as the decimal it is
  written as; one batch entry per item in order, keyed by `test`; the total
  under `out/` within `output_mb` per item; and per item an `outcome` a step
  may return, only outputs the plan declares for the step, each of its
  declared type (a file a regular file under `out/` and a folder a directory
  there, reached without a symbolic link, a number finite), every output
  without a `?` present on `accepted`, and every number the report reads
  within its `at_least` and `at_most`, compared exactly as written
  (`1.0000000000000000001` is past `at_most: 1`). Any of these failing is a
  system error.
- A step's container killed at its time limit, or one that wrote nothing
  after the kernel killed it at its memory limit, is a system error naming
  the step, whether it runs once, for one test or as a batch. The primitive
  keeps a contestant's program within the test's own limits and says how it
  ended, so a container that outran its own limits is a fault of the
  platform or of setter code, never the test's `time_limit`. A step that
  wrote a valid `outputs.json` is taken at its word even when Docker reports
  an out-of-memory kill inside it, because under `sandbox-run` that kill is
  the contestant's program.
- This machine gives a step no network and no GPUs, so a step whose plan entry
  says `network: true` or `gpus` above 0 is a system error before its
  container is made.
- The result: a row per test in plan order. Its outcome is the test's first
  non-accepted outcome; otherwise `accepted` when the run went to the end, or
  when a system error struck after every per-test step of the test had run
  for it; otherwise `skipped`. Its `values` are every per-test name of the
  report whose step ran for the test and wrote the output, a wrong answer's
  included; a skipped test has none. The once `values` are every once name
  whose step wrote the output, kept when a system error struck later. A
  number is written exactly as the primitive wrote it; a text has every
  secret's value replaced by `***` and is cut at 10,000 characters.

### The two logs

The run log, the one object the harness stores, is for the task's
organisers: it names every test. It says what ran and how it went, and
nothing about the machine:

```
[    0.000s] Grading submission/3, attempt 1.
[    0.412s] 5 steps to run over 3 tests.
[    1.803s] compile (unicon/compile@v2): accepted
[    3.120s] run (unicon/sandbox-run@v2): 3 tests
[    3.120s]   test main/1: accepted
[    3.120s]   test main/2: time_limit
[    3.121s]   test main/10: accepted
[    3.566s] check for test main/1 (unicon/diff-check@v2): accepted
[    3.566s] check for test main/2 (unicon/diff-check@v2): skipped, test main/2 is already time_limit
[    4.010s] check for test main/10 (unicon/diff-check@v2): accepted
[    4.011s] 2 of 3 tests accepted.
```

No image, volume, container, exit code or path, and never what a step printed:
a check could print the expected answer, and a program's own output could
carry a hidden test's input. A system error says only that there was one.
Everything else goes to the harness's stdout, the CI's own job log, for
staff: the envelope's identity, the workspace volume, each step's image and
exit, what each step printed (cut to 16 KB), callback trouble and tracebacks.
The run log's lines are there too, so the CI log reads as the whole story.
Neither log carries the callback token, a presigned query or a secret's
value.

### Every step container

Created through the filter from the step's image by digest, with:

| Setting | Value |
|---|---|
| Program | the image's own entrypoint; the harness sets no command |
| Network | `NetworkMode: none` |
| First process | `Init: false`, so the step's own program is the container's first process whatever the daemon's default |
| Shared memory | `IpcMode: none`: no `/dev/shm`, so `/work` and `/tmp` are the only places a step writes |
| Root filesystem | read-only |
| Capabilities | all dropped |
| Security options | `no-new-privileges` and `seccomp=builtin`, named explicitly because a daemon's default can be unconfined |
| User | `10001:10001`, the harness's own uid, which owns the step directory |
| Memory | `memory_mb`, with `MemorySwap` equal to it: no swap |
| CPU | one CPU (`NanoCpus`), and a `cpu` ulimit of `cpu_ms` rounded up to seconds per process |
| Processes | `PidsLimit: pids` |
| Output | an `fsize` ulimit of `output_mb` per file |
| Wall clock | `time_ms` and a start allowance of 10 seconds for the container's own start, after which the harness kills it; the label `unicon.time_ms` carries the sum for the filter's reaper |
| `/work` | the step's own directory, `unicon-steps/<n>-<step>` of the run's workspace volume, as a volume subpath mount with `NoCopy`, read-write |
| `/tmp` | a tmpfs, at most 256 MB and never more than the memory limit |
| Labels | `unicon.grading=<grading id>`, `unicon.step=<step id>`, `unicon.time_ms`; the filter adds `unicon.harness` |
| Log | the `json-file` driver, at most 8 MB in one file, so what a step prints cannot fill the machine's disk |

The harness removes every container it created in a `finally`, and the filter
removes any it misses.

The escape fixtures keep this set honest (`harness/tests/test_escape.py`,
`harness/tests/escape/escape.c`). One C program, built by the compile
primitive, tries every way out: memory, processes, threads, CPU, sleeping,
output and disk; the network, the cloud metadata address and name lookup;
writing outside its directory and reading other tests' files, the task's
answers and the sockets; `ptrace`, `mount`, `unshare`, `bpf`, `keyctl`,
loading a module and `setuid(0)`; its own capabilities, NoNewPrivs, seccomp
mode and uid; and any secret in reach. It runs in containers made from
exactly the body above, against the released primitives by digest: through
sandbox-run, where every attack is refused; as the container's own process,
where everything is refused but the step's own `/work`; and with each of
the eleven protections taken away in turn, each of which lets a named attack
through. A field added to the body fails a test until it is either given a
removal and an attack that shows it gone or said not to be a protection.
The fixtures speak the contract the image speaks: each image is asked in
contract version 5 first and, when it answers that it speaks another
version, in version 4, the version of the pinned releases; every output is
read at the path the primitive reports. They write their own `inputs.json`,
since they test the container the harness builds, not the files it writes.
CI runs them in a job of their own, and sandbox-run's CI runs them against
the image it just built (`SANDBOX_RUN_IMAGE`, `COMPILE_IMAGE`).

### The host-path trap

The daemon resolves a mount's source on the machine, not inside the harness, so
a directory the harness made at its own `/woodpecker/x` means nothing to it:
bind-mounting it gives the step an empty directory and a silently wrong
result. The harness therefore finds its own container id in
`/proc/self/mountinfo`, inspects itself through the filter, takes the Docker
volume mounted over the task checkout, and gives each step a directory of that
volume as a volume subpath mount. A volume and a subpath are something the
filter can check against the caller's own mounts. Subpath mounts need Docker 26
or later (Engine API 1.45).

## The machine contract

Whatever starts a grading run, today Woodpecker, owes the machine and the
harness the following, and a CI put behind the `forge` repo's grading port
in its place is checked against this list. What runs is decided by the
platform from its own records, never by the request or the repository; that
rule is the `forge` repo's to keep, and the list here is the rest.

1. **The org's clone credential reaches only the checkout steps**, and it
   reads only that org's repositories. The harness step gets no credential
   of any kind. Today: the clone image is on the CI's trusted-clone list,
   which lends the activating account's credential to clone steps alone,
   and that account is a member of its own org and nothing else.
2. **A run goes only to a machine carrying the run's label**
   (`pool:platform` until orgs bring their own machines). Today: the run's
   `labels`, matched against the agent's.
3. **The harness container is fixed:**
   - the harness image by the digest in the plan;
   - the socket filter's folder mounted read-only at `/run/unicon`, and
     `DOCKER_HOST=unix:///run/unicon/docker.sock`;
   - `UNICON_ENVELOPE_URL` and `UNICON_GRADING_ID` in its environment;
   - no credential and no other socket.
4. **Both checkouts are inside one Docker volume, mounted into the harness
   container where the socket filter looks for it** (`UNICON_FILTER_WORKSPACE`,
   `/woodpecker` by default), since that mount is how the filter tells one
   run from another. The task is checked out at its publication's commit
   and the submission at its own, each with its big files, since every file
   a person uploads is one. The harness finds that volume from its own
   mounts and gives each step a subpath of it (the host-path trap, above),
   and the envelope tells it where each checkout is. Today: the CI's
   workspace volume, at `/woodpecker/task` and `/woodpecker/submission`.
5. **The big files come through a store of the org's own on the machine**
   (`unicon-lfs-<org>`, mounted at `/lfs-cache` in both checkouts), so no
   org's checkout is served a file another org's brought to the machine by
   naming its object id. Today: a volume per org, named in the CI's answer.

## The socket filter

`python -m unicon_filter`, standard library only. It listens on
`/run/unicon/docker.sock`, forwards to `/var/run/docker.sock`, and reads every
request on every connection, kept-alive and pipelined ones included. It
passes:

- `GET /_ping`, `HEAD /_ping`, `GET /version`;
- `POST /containers/create` for a grading step (below);
- start, wait, kill, stop, inspect, logs (not followed) and delete on a
  container this filter created for the calling harness, addressed by its
  full id;
- inspect of the caller's own container, which is how the harness finds its
  workspace volume.

Everything else is a 403 with `{"message": "unicon socket filter: <reason>"}`,
and the connection closes. Every decision is printed as one JSON line.

**Who is calling.** Every harness on a machine shares the one socket, so the
filter tells runs apart by the process that connects: the kernel gives it that
process as a pidfd (`SO_PEERPIDFD`, falling back to `SO_PEERCRED` before Linux
6.5), its cgroup names its container, and the daemon says what that container
is: its `UNICON_GRADING_ID` and the volume it has at `/woodpecker`. This needs
the filter to run with `pid: host`. A connection is identified once, when it
opens. The cgroup is taken only in the shapes Docker makes, with the id as
the last segment: `/docker/<id>` under cgroupfs (Docker Desktop's too),
`<slices>/docker-<id>.scope` under systemd, and either seen from the filter's
own cgroup namespace, `/../<id>` or `/../docker-<id>.scope`. A cgroup deeper
than the container's names none, since a process could make one below its own
and name it after another container; under cgroup v1, lines naming two
different containers name none. A
create must carry that grading id as its `unicon.grading` label and may mount
only that volume, at a `unicon-steps/` subpath, so one run can neither mount
another run's workspace nor touch its steps, and no step sees the checkouts.
The filter labels every step it lets through with `unicon.harness`, the
calling container's id, and only that harness may touch the step afterwards,
so not even a second run of the same grading reaches the first one's steps.

**A create.** The body is read against the exact list of fields the Engine API
knows, because the daemon's JSON decoder ignores the case of field names; any
other spelling, and any repeated key, is refused. It must carry an image from
`UNICON_FILTER_IMAGES`, a numeric non-root `uid:gid`, the two labels and at
most `unicon.step` besides (no other label, so a step cannot pass for another
program's container), no container name, the full set of flags in the table
above, a memory, CPU and pids limit within the
machine's ceilings, only the `cpu` and `fsize` ulimits, and exactly the `/work`
mount and at most the `/tmp` tmpfs, and a `json-file` or `local` log with
`max-size` and `max-file` set and at most 16 MB kept in all (the machine's
default driver may keep everything). `NetworkingConfig` names no endpoint.
Every other field must be empty. That
refuses, among others: privileged, any bind mount, the daemon's socket, a
network, added capabilities, other seccomp or AppArmor profiles, host
namespaces, devices, sysctls, ports, restart policies, other runtimes, emptied
`MaskedPaths` or `ReadonlyPaths`, anonymous volumes, and a volume driver's
options. A list counts as empty only when it has no entries (or only zeros,
as the CLI's `ConsoleSize`): `Binds: [""]` is refused. Where the filter only
asks whether a structure is empty, it compares field names without regard to
case, as the daemon does: `{"endpointsconfig": {"host": {}}}` names a network. The daemon is sent the
document the filter judged, written out again with the harness label added,
never the caller's own bytes.

**What it reads strictly.** A bare CR or LF, a folded, malformed or repeated
header, any `Transfer-Encoding`, `Expect` or `Upgrade`, a percent-encoded path
or query, a `Content-Length` that is not plain ASCII digits, and a body on
any verb but create are refused. The daemon is sent a request the filter
writes afresh from what it judged, and the harness gets the daemon's answer
back framed by `Content-Length`.

**Ceilings.** One calling container may hold at most
`UNICON_FILTER_MAX_CONNECTIONS` connections at once, and one grading at most
`UNICON_FILTER_MAX_STEPS` step containers on the machine; the harness runs
one at a time and removes each before the next. A create counts against its
grading from the moment it is judged, before the daemon has answered, so
creates sent at once on many connections stop at the ceiling too; one the
daemon refuses gives its place back.

**The reaper.** Every five seconds the filter kills and removes a container it
created that has outlived its `unicon.time_ms` plus a grace period, or whose
harness container is gone. At start, and on each pass, it takes on containers
carrying both of its labels that it has no record of, which is what a
restarted filter finds, and knows each one's harness by its `unicon.harness`
label.

| Variable | Default | What it sets |
|---|---|---|
| `UNICON_FILTER_IMAGES` | required | Image references by digest, separated by commas or white space. The list cannot change while the filter runs |
| `UNICON_FILTER_UPSTREAM` | `/var/run/docker.sock` | The daemon's socket |
| `UNICON_FILTER_LISTEN` | `/run/unicon/docker.sock` | The filter's own socket, mode 0666 |
| `UNICON_FILTER_SOCKET_CHECK_SECONDS` | `1` | How often the filter checks that the socket at that path is still the one it bound |
| `UNICON_FILTER_WORKSPACE` | `/woodpecker` | Where the caller has the run's workspace volume |
| `UNICON_FILTER_GRACE_SECONDS` | `30` | Added to a step's clock before the reaper takes it |
| `UNICON_FILTER_REAP_EVERY_SECONDS` | `5` | How often the reaper looks |
| `UNICON_FILTER_MAX_MEMORY_MB`, `_MAX_PIDS`, `_MAX_CPUS`, `_MAX_TIME_MS` | 16384, 4096, the machine's CPUs, 6 hours | The ceilings a create may ask for |
| `UNICON_FILTER_MAX_STEPS` | 4 | Step containers one grading may have on the machine at once |
| `UNICON_FILTER_MAX_CONNECTIONS` | 32 | Connections one calling container may hold at once |

The image runs as uid 10002, which no harness (10001) or step runs as. Start
it the same way on rootless Docker and on root Docker with `userns-remap`:

- `--userns host` (compose `userns_mode: host`), outside any remap of user
  ids. A daemon that remaps refuses the machine's pid namespace to a
  container that has not opted out ("cannot share the host PID namespace
  when user namespaces are enabled"); on a daemon that does not remap it
  changes nothing. The filter is the platform's own code, so running it
  outside the remap gives up nothing the remap protects.
- `--pid host`.
- The daemon's socket at `/var/run/docker.sock` (rootless Docker's is
  `$XDG_RUNTIME_DIR/docker.sock`) and the group that owns it (`--group-add`,
  compose `group_add`), as a container outside the remap sees it.
- A directory on the machine at `/run/unicon`, not a Docker volume, owned by
  uid 10002 and mode 0755, such as `/run/unicon-filter`. A daemon that
  remaps keeps its volumes under `/var/lib/docker/<uid>.<gid>/`, mode 0710,
  which uid 10002 outside the remap cannot enter. Make the directory with a
  container on the same daemon, `docker run --rm --userns host -v
  /run/unicon-filter:/d busybox chown 10002:10002 /d`: the daemon creates a
  missing bind source itself, where it sees paths (rootless Docker has a
  `/run` of its own), and the chown is in its uid mapping (under rootless
  Docker uid 10002 is one of the user's sub-uids on the machine). `/run` is
  emptied when the machine starts, so this runs before every start of the
  filter.

Give every harness that directory read-only
(`/run/unicon-filter:/run/unicon:ro`): the harness starts as root, and with
the directory writable it could remove the socket and bind its own in its
place. Under a remap a harness sees the socket as belonging to `nobody` and
cannot write the directory at all. The filter checks that the file at its
socket's path is still the socket it bound, and when it is not it exits 3
with `unicon-socket-filter: socket_replaced: ...` on stderr, so the machine
restarts it and notices. It exits 2 with one line on stderr,
`unicon-socket-filter: <code>: <message>`, when its settings are wrong
(`missing_environment`, `bad_setting`) or the daemon does not answer
(`upstream_unreachable`).

The filter is not a sandbox: a step still runs on the machine's kernel.

## The clone image

`woodpeckerci/plugin-git` 2.10.1 by digest plus two pieces of system git
config and a lock around the download, which together are all the platform
needs from a checkout.

`lfs.storage = /lfs-cache`. The CI's checkout steps mount a volume the machine
keeps at `/lfs-cache`, one per org (the `forge` repo's CI answer names it,
`unicon-lfs-<org>`), so a dataset is downloaded once per org and machine and
no org's checkout is served an object another org's brought by naming its id;
the checkout still copies it into the run's workspace.

`core.attributesFile`, naming a file that says every path may be a large
file. Every file a person uploads is an object in the forge's store and the
commit holds a pointer to it, and git-lfs resolves a pointer at checkout only
where an attributes file says that path is one. Without this the workspace
gets the pointer's text where the file should be, the step still exits 0, and
the grading fails as though the contestant had submitted that text. The rule
cannot live in the repositories, because the forge's file API applies a
repository's own attributes to everything written through it and would store
a pointer committed under one as a second object. The pattern is every path
rather than the places uploads land, so no convention has to be kept in step
between this image and the platform; a file that is not a pointer passes
through unchanged, with a line in the step's log saying it was not one, and
git-lfs keeps a copy of it in the cache.

A lock around `git lfs fetch`. The plugin checks a commit out with its
pointers, then downloads the missing objects into the cache with `git lfs
fetch` and copies them into the workspace with `git lfs checkout`. git-lfs
does not make one process wait for another's download, so gradings that
started together on a cache without the dataset each downloaded it (eight at
once made eight downloads, experiment D2 of 2026-10-04). `git-lfs` in the
image is a script in front of the real one that takes an `flock` on
`/lfs-cache/fetch-locks/<commit>` for the length of the fetch: the first
checkout of a commit downloads, the others wait and then find every object
there, and all of them copy out side by side. The key is the commit because
a commit names every pointer in it, so two checkouts of one commit want
exactly the same objects; a lock per object would need the list of objects
before the fetch and one lock for each, and every uploaded file is an
object. A checkout that dies lets go of the lock with its last process. The
lock files, one empty file per commit, stay in the cache.

The image names no volume, and runs as root as plugin-git does, so a volume
Docker makes on first use needs no preparing. Both settings are in the image
because a clone step given an `environment` block stops counting as a plugin
and is no longer lent the credential it clones with. The image goes on the
CI's trusted-clone list beside plugin-git.

## The contracts

The five share one `schema_version`, 5, the one a runner release publishes them
at. A release that changes the shape of any of them raises it, and the harness
refuses a file written for another. `scripts/check_contract_files.py` fails CI
when a file pins another version, when an example under `examples/` does not
validate, or when a schema field is one no example sets.

**`plan.schema.json`.** Written by the `forge` repo's compiler at every valid
save as `plans/plan.json` in the task repo, one per task; read by the harness
only. Flat and fully resolved: `harness_image` by digest, `tests` (every test
id, `<group>/<test>`, groups in name order and tests in natural order),
`contestant` (the workflow's contestant inputs, `{type, options, per_test}`),
`steps`, and `report`. A step has `id`, `primitive` (`owner/name@version`),
`image` by digest, `network`, all six `limits` (`gpus` among them),
`outputs` (every declared output port and its type, a `?` after the name of
one an accepted result may leave out, `outcome` always), `folders` (its
input ports of type folder), and is one of three shapes: runs once
(`inputs`), runs for one test (`inputs` and `test`), or one container for
many tests (`batch`, a list of `{test, inputs}`). An input value is exactly
one of `{"value": ...}`, `{"task": path}` (a folder when the path ends in
`/`), `{"submission": id}`, `{"secret": name}`, `{"step": id, "output":
name}` and `{"template": text, "parts": [{"submission": id}, ...]}`; no value
names a test, since inside an entry for one test a per-test input or step
gives that test's value. `report` is the workflow's report, each name a step
and an output with the `at_least` and `at_most` a number keeps. Beyond the
schema the harness checks that a step id names one step or the entries of
one per-test step, that every step that runs once comes before every step
that runs per test, that every test named is in `tests`, that every
reference points at an output declared by another step that runs earlier
and that no step that runs once reads one that runs per test, that every
contestant input named is declared and of the kind its value needs, that
every template is well formed, and that the report reads a declared text or
number output of a step the plan has.

**`envelope.schema.json`.** Served by the `forge` repo at the run's envelope
URL. `grading_id`, `submission`, `attempt`, `checkouts` (`/woodpecker/task`,
`/woodpecker/submission`), `callback` (`url` and a one-run `token`),
`log_put` (a presigned PUT into `unicon-results` at
`logs/<grading id>/<attempt>.log`), `deadline`, `limits.wall_seconds`, which
is always present, and `secrets`, the value of every secret the plan names,
by name (`{}` when it names none). The run's wall clock is the smaller of
`wall_seconds` and the time to the deadline less 30 seconds kept for
reporting.

**`result.schema.json`.** Posted in the final callback; checked by the
`forge` repo and kept on the grading row. `stopped` (null, the outcome of a
step that runs once and stopped the run, or `system_error`), `stopped_by`
(the id of that step when one stopped the run, otherwise null, a
`system_error` included; the platform holds a stop back until the task's
reveal only when this step is sealed), `tests` (one row per plan test, in
plan order, `{test, outcome, values}`), `values` (the once names of the
report), `run_log` (the log's URL without its presigned query, or null) and
`error` (a sentence for staff exactly when `stopped` is `system_error`). The
outcomes are `accepted`, `wrong_answer`, `time_limit`, `memory_limit`,
`output_limit`, `runtime_error`, `compile_error`, `skipped` and
`system_error`; a step returns neither of the last two, and a row is
never `system_error`. A value is a number exactly as the primitive wrote it,
or a text of at most 10,000 characters. The callback URL and its token name
the grading, so the result does not. Every other outcome, and every number
shown, the platform works out from the rows.

**`primitive.schema.json`.** How the harness and a primitive image talk, and
the `primitive.yaml` declaration the forge compiler reads; a document is
exactly one of the three.

| File | Shape |
|---|---|
| `inputs.json` | `{"schema_version": 5, "inputs": {...}}`, or `"batch": [{"test": id, "inputs": {...}}]` |
| `outputs.json` | `{"schema_version": 5, "outputs": {...}}`, or `"batch": [{"test": id, "outputs": {...}}]` one per item in the same order, or `{"schema_version": 5, "error": "one sentence"}` when the primitive could not work at all |
| `primitive.yaml` | `image` by digest, `batch`, `network`, `limits` (all six), optional `limits_from` (`{input, scale, add}`, scale 1 and add 0 when left out), `inputs` and `outputs` as `{type, options, optional, runs, secret}` |

A value is text, a number, a boolean, a file `{"file": "in/..."}` or
`{"file": "out/..."}`, or a folder `{"folder": "in/..."}` or `{"folder":
"out/..."}`, the tree under it with its layout. Paths have no empty, `.` or
`..` segment. A test id holds a `/`, so a primitive that writes a file per
test names it some other way, such as by the item's index. Types are `text`,
`number`, `boolean`, `enum` with `options`, `file`, `folder`, and `outcome`
for the one output every primitive declares. Every `file` or `folder` input
says with `runs` whether the primitive runs what arrives there as a program,
and `secret` marks an input the image keeps from any program it runs. The
declaration names neither the primitive nor its version: the repo at the
forge is the name and its tag the version. The container runs the image's
own `ENTRYPOINT`, so a primitive's Dockerfile sets one in exec form and its
declaration names no program. A primitive repo validates its
`primitive.yaml` against the release's schema with a placeholder image
filled in, since bootstrap writes the image (below).

**`submission.schema.json`.** `submission.json` at the root of a submission
commit: `{"schema_version": 5, "inputs": {id: entry}}`, one entry per
contestant input the plan declares, where an entry is `{"files":
["files/<id>/<path>", ...]}` for a `file` input (one file), a `folder` input
(its files at their paths in the folder) or a per-test input (one file per
test it answers, `files/<id>/<group>/<test>` or
`files/<id>/<group>/<test>.<ending>` with an ending that is not empty), or
`{"value": ...}` for `text`, `number`, `boolean` and `enum`. The harness
refuses one that leaves out an input, gives one the plan does not declare,
gives files for a value or a value for files, or gives a per-test file named
neither way or two files for one test. A per-test file for a test the plan
does not have, which a rejudge against a plan that dropped or renamed the
test meets, is noted in the CI log and not read. A `{"submission": id}` plan
value gives the one file of a file input, the files of a folder input as one
folder, the test's file of a per-test input, or the value.

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
  For `primitive-sandbox-run`, that job also runs this repo's escape
  fixtures against the image it built.
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
primitive builds, sandbox-run: both workflows build `primitive-compile` from
its `main` branch and hand it to the image tests as `COMPILE_IMAGE`.

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

**The harness runs on machines we do not own.** It never talks to Forgejo. It
calls only the platform, at the envelope and callback routes the backend
serves over the `forge` repo's package, and puts its log by the presigned URL;
the only credential it ever holds is the callback token for its one run, which
dies at the deadline. The token and the presigned queries never reach the run
log or the CI log.

**Every plan and envelope carries `schema_version`, and a mismatch is refused.**
A compiler that emits a shape the harness reads differently produces a wrong
result, not an error, and a wrong result is the one failure nobody notices.

**No image is ever released as `:latest`.** Plans pin the harness image by
digest so a task keeps grading the way it was published, and a grading machine
pins the other three the same way. The base images are pinned by digest too.

## Layout

```
harness/unicon_harness/          the harness
harness/tests/                   its tests; primitives/ holds the fixture image, one
                                 program that stands in for every primitive, and
                                 escape/ the escape fixtures' C program
socket_filter/unicon_filter/     the socket filter
socket_filter/filter_tests/      its tests, and the Docker lab the integration tests use
images/<name>/Dockerfile         the four images
images/clone_tests/              the clone image's Docker test and its small git-lfs server
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

`uv run pytest` runs the unit tests; a few that need unix sockets run on Linux
only. `uv run pytest -m docker` needs a Docker daemon (26 or later): it builds
the images, starts a registry of its own on `127.0.0.1:5056` to give the
fixture image a digest, and runs a whole grading through the harness and
the filter, the filter's escape checks, one run trying to reach another's steps,
the reaper, the filter noticing its socket replaced, and checkouts with the
clone image against a small git-lfs server: a second checkout sharing the
first's cache must download nothing, one with a cache of its own must
download the file again, and four started together on an empty cache must
download it once between them. Everything it makes is named `unicon-lab-*`
and removed at the end (the filter's socket directories are under
`/run/unicon-lab` on the daemon's machine), except the registry container,
`unicon-lab-registry`, which later runs reuse. It also runs the escape fixtures, which pull the released compile
and sandbox-run images and talk to the daemon's own unix socket
(`DOCKER_HOST` when it names one, else `/var/run/docker.sock`), so they
skip on a machine whose daemon is reached another way; on their own,
`uv run pytest -m docker harness/tests/test_escape.py`. With `DOCKER_HOST`
naming rootless Docker's socket the whole set runs against that daemon.

CI runs those commands, checks that the Dockerfiles on the python base share
one digest, builds the four images, and starts each once: the harness and the
filter with nothing set, to see them refuse with exit 2, the clone image to see
`git config --system lfs.storage` is `/lfs-cache`, and the worker to see its
placeholder.

```
docker build -f images/harness/Dockerfile -t unicon-harness:dev .
docker build -f images/socket-filter/Dockerfile -t unicon-socket-filter:dev .
docker build -f images/clone/Dockerfile -t unicon-clone:dev .
docker build -f images/worker/Dockerfile -t unicon-worker:dev .
```

When the run never begins the harness exits 2 and prints one line to stderr
that starts with `unicon-harness: <code>:`:

| Code | What happened |
|---|---|
| `missing_environment` | `UNICON_ENVELOPE_URL` or `UNICON_GRADING_ID` is not set |
| `envelope_unreachable` | The envelope URL did not answer, answered an error, or is not a URL |
| `envelope_not_json` | What came back is not a JSON object |
| `schema_version_mismatch` | The envelope is written against a version this image does not speak |
| `schema_violation` | The envelope does not match `envelope.schema.json` |
| `grading_id_mismatch` | The envelope is for a different grading run than the environment says |
| `schemas_missing` | The image was built without the contract files. A packaging fault, not a job fault |

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks the tag against the version in `pyproject.toml`, runs
the same checks as CI, pushes the four images as `ghcr.io/uniconhq/harness:v1.2.3`,
`socket-filter:v1.2.3`, `clone:v1.2.3` and `worker:v1.2.3`, and creates a GitHub
release with the five contract files and `images.json` attached, which names
each image by digest, and the same four digests in the notes. `deploy` pins
that release and those digests.

The first push creates each package on the organisation as **private**,
whatever the repo's visibility is. Grading machines pull them anonymously, so
someone has to open each of the four packages on the organisation's Packages
page once and set its visibility to public, and add the `runner` repo under
Manage Actions access so later releases can keep pushing to it. Until that is
done the first `docker pull` from a machine fails with a 403 that reads like a
missing tag.
