#!/usr/local/bin/python3 -I
"""The step wrapper, `unicon-step`: the entrypoint of every primitive image
built on ghcr.io/uniconhq/primitive-base, and all a Woodpecker step does
besides the primitive's own program, which is the image's command.

A grading step is a plugin step with two settings: `data_url`, the step's
door at the forge, and `data_token`, which goes only in an Authorization
header and only to the door and the links under it. In order, the wrapper:

1. makes itself non-dumpable, so no process of its user reads its
   environment through /proc/1/environ, nor its memory;
2. fetches the step's job (schemas/job.schema.json), or, answered 204,
   exits 0 having done nothing: nothing is left for the step to do, and
   the later steps still run;
3. places the job's files, each checked against its length and SHA-256:
   into /work/in, or a cached one through the machine's cache at /cache,
   linked into an item folder of this running step's own, and makes each
   empty folder the job names;
4. writes /work/inputs.json with each secret's value filled in, and runs
   the primitive with no PLUGIN_ variable in its environment, its box
   users in UNICON_BOX_USERS and its memory in UNICON_STEP_MEMORY_MB,
   killing it at the job's time limit, and, as the container's first
   process, everything else left once it ends;
5. masks every secret, the link and the token in what the primitive wrote
   and printed, uploads /work/outputs.json and /work/out, each PUT sending
   the file's SHA-256 its link is signed for, and tells the door the step
   is done;
6. exits 1 when the step did not do its work, or when the job says a
   non-accepted outcome fails the step and one is not accepted.

Whatever stops the step once the job came is written as the error of
outputs.json and uploaded, so the forge reads one place. Exit 2 is the
wrapper not reaching the forge. With the setting `mode: report` it only
tells the door the pipeline ended. With no data link it is not in a
grading step and runs the command in its place, as the primitives' own
tests start an image, unless it is in a Woodpecker step, where the forge
writes a link for every step it runs this image in.

It follows no redirect and uses no proxy: every link it is given is the
forge's or the store's, reached directly.
"""

import base64
import codecs
import concurrent.futures
import contextlib
import ctypes
import errno
import fcntl
import functools
import hashlib
import http.client
import json
import os
import platform
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

JOB_VERSION = 1
WORK = Path("/work")
CACHE = Path("/cache")
CACHE_CAP_FILE = Path("/etc/unicon/cache-cap")
"""Where a machine may set its cache's cap in bytes; else `CACHE_CAP`."""
CACHE_CAP = 10 * 1024**3
DISK_FREE_SHARE = 0.1
"""The share of the cache's disk eviction keeps free. The cap is one org's
cache, and the disk holds every org's on the machine, so two orgs within
their caps could still fill it: eviction goes on while less than this is
free, though the org is within its cap."""
STALE_SECONDS = 3600
"""How old a lock being made, a step folder without a lock, or a folder a
program was being compiled into, must be before a sweep takes it for one a
killed step left."""
PROGRAM_KEY = re.compile(r"[0-9a-f]{64}")
"""The name of a compiled program's folder in /cache/programs, its key."""
ENV_NAME = re.compile(r"[0-9a-f]{64}-[0-9a-f]{64}")
"""The name of an environment's folder in /cache/envs: the fingerprint of
the lock it was assembled for and the toolchain line of the exec that
assembled it."""
NAMES = {"programs": PROGRAM_KEY, "envs": ENV_NAME}
UV_ARCHIVES = "uv/archive-v0"
"""The folder of uv's cache each folder in which is a wheel it unpacked."""
STORE_ENTRIES = ("conda", UV_ARCHIVES, "wheels", "files")
"""The folders of /cache/store each folder in which is one store entry: an
unpacked conda package in rattler's cache, an unpacked wheel in uv's, a
wheel file as exec hands it to uv, and a Maven jar or an unpacked npm
package as exec places it, its folders and files read-only. Everything
else in the store is a tool's own bookkeeping, which eviction leaves alone
but for `UV_POINTERS`."""
UV_POINTERS = "uv/wheels-v7"
"""Where uv keeps, for each wheel it unpacked, a `.rev` file naming the
unpacked entry under `uv/archive-v0`. uv trusts a pointer without looking
whether its entry is there, and fails an install whose entry is gone, so
an entry is freed with the pointers naming it, before it."""
UNFINISHED = {
    "files": re.compile(r"\.[0-9a-f]{64}\..+"),
    "uv": re.compile(r"\.tmp[A-Za-z0-9]+"),
}
"""What a step killed while it wrote the store leaves there, by folder:
exec's Maven jar or npm package unpacked under `.<sha256>.<random>` before
its rename into `files/`, and the folder uv unpacks a wheel into,
`.tmp<random>` in its cache's root, before its rename into `UV_ARCHIVES`.
exec's copy of a wheel, `.<file name>.<random>`, is inside the wheel's
entry under `wheels/`. rattler unpacks a conda package in place, under its
own lock, and checks it before it trusts it, so it leaves none."""
POINTER_LIMIT = 1 << 16
"""The most of a uv pointer read: uv writes a few names and times."""
READY_LIMIT = 1 << 20
"""The most of an environment's `ready.json` read: exec writes a few
fields and the store entries the folder links."""
FOLDER_SIZES: dict[tuple[str, int, int], int] = {}
"""Each compiled program's, environment's or store entry's folder size the
step has counted, by its path and its own inode: a folder under its name is
renamed into place whole and never changed, so it is walked once a step,
not once for each piece the step fetches."""
PARALLEL = 8
TRIES = 4
BACKOFF = 1.0
"""Seconds before the first retry, doubled for each after."""
CHUNK = 1 << 20
TIMEOUT = 60
PR_SET_DUMPABLE = 4
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}")
SETTING = re.compile(r"[a-z][a-z0-9_]*")
OWN_SETTINGS = frozenset({"data_url", "data_token", "mode"})
"""The wrapper's own settings, which no secret may be read from."""
SHORTEST_SECRET = 8
"""Fewer characters than this and a secret would be masked inside ordinary
words and numbers, so a step given one fails instead."""
JSON = "application/json"
BYTES = "application/octet-stream"
DEFAULT_PORTS = {"http": 80, "https": 443}


class StepError(Exception):
    """What kept the step from doing its work, a sentence for staff."""


class RefusedError(StepError):
    """An answer of 4xx but 429, which no try made again would change."""

    def __init__(self, method: str, code: int) -> None:
        super().__init__(f"{method} answered {code}")
        self.code = code


class RetryError(Exception):
    """A try that may pass when made again: no answer, a cut answer, a 429
    or a 5xx. `retried` turns the last one into a `StepError`."""


class Number(str):
    """A JSON number as the primitive wrote it, so it is written back so."""


def main(argv: list[str]) -> int:
    url = os.environ.get("PLUGIN_DATA_URL")
    if not url:
        if os.environ.get("CI") == "woodpecker":
            print(
                "unicon-step: a Woodpecker step without the setting data_url; the "
                "forge gives one to every step it runs this image in",
                file=sys.stderr,
            )
            return 2
        if len(argv) < 2:
            print("unicon-step: no data link and no command", file=sys.stderr)
            return 2
        os.execvp(argv[1], argv[1:])
    undumpable()
    door = Door(url, os.environ.get("PLUGIN_DATA_TOKEN", ""))
    try:
        if not door.token:
            raise StepError("the step has a data link and no data token")
        if os.environ.get("CI") == "woodpecker" and os.getpid() != 1:
            raise StepError(
                "the wrapper is not the container's first process, so it could "
                "not kill what a primitive leaves running"
            )
        if os.environ.get("PLUGIN_MODE") == "report":
            door.call("POST", url, {}, ends_token=True)
            say(door, "unicon-step: told the forge the pipeline ended")
            return 0
        job = read_job(door, url)
        if job is None:
            say(door, "unicon-step: the forge has nothing left for this step to do")
            return 0
        if not isinstance(job, dict) or job.get("schema_version") != JOB_VERSION:
            raise StepError(f"the job is not written against version {JOB_VERSION}")
        return grade(door, job, argv[1:])
    except (
        StepError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        http.client.HTTPException,
    ) as error:
        say(door, f"unicon-step: {type(error).__name__}: {error}")
        return 2


def read_job(door: Door, url: str) -> Any:
    """The step's job, or none for a 204: its inputs with each number as
    written, so inputs.json carries it exactly, as TASK-FORMAT 1.6 asks,
    and the rest read as Python numbers for the wrapper's own use. The
    request names the machine's conda platform, which only a step given an
    environment needs; on a machine of no platform it knows it names none,
    and the forge refuses such a step itself."""
    selected = {
        "x86_64": "linux-64",
        "amd64": "linux-64",
        "aarch64": "linux-aarch64",
        "arm64": "linux-aarch64",
    }.get(platform.machine().lower())
    parsed = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query)
        if key != "platform"
    ]
    if selected is not None:
        query.append(("platform", selected))
    url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
    job = door.call("GET", url, exactly=True)
    if not isinstance(job, dict):
        return job
    inputs = job.pop("inputs", None)
    job = json.loads(exact(job))
    if inputs is not None:
        job["inputs"] = inputs
    return job


def grade(door: Door, job: dict[str, Any], command: list[str]) -> int:
    """Steps 3 to 6 for one job, and the exit code."""
    outputs = WORK / "outputs.json"
    try:
        secrets = read_secrets(job.get("secrets", []))
        door.add_masks(secrets.values())
        with Cache.for_step(job["files"]) as cache:
            place(door, job["files"], cache)
            filled = fill_secrets(job["inputs"], secrets)
            (WORK / "inputs.json").write_text(exact(filled), encoding="utf-8")
            ended = run(door, command, job)
        document = mask(door, read_plain(outputs, ended))
    except (StepError, OSError, ValueError, http.client.HTTPException) as error:
        version = job["inputs"].get("schema_version")
        document = {"schema_version": version, "error": door.masked(str(error))}
        write_outputs(exact(document))
    upload(door, job)
    door.call("POST", job["done"], {}, ends_token=True)
    if "error" in document:
        say(door, f"unicon-step: the step failed: {document['error']}")
        return 1
    if "batch" in document:
        found = [item.get("outputs") for item in document["batch"]]
    else:
        found = [document.get("outputs")]
    refused = [
        str(outcome or "missing")
        for o in found
        if (outcome := o.get("outcome") if isinstance(o, dict) else None) != "accepted"
    ]
    if job["fail_on_non_accepted"] and refused:
        say(door, f"unicon-step: the outcome is {refused[0]}; no later step runs")
        return 1
    return 0


def undumpable() -> None:
    """Make this process non-dumpable: its /proc/self/environ and memory are
    then root's, so a process of its user, a compile's, cannot read them."""
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise SystemExit("unicon-step: could not make itself non-dumpable")


def read_secrets(names: Any) -> dict[str, str]:
    """The value of each secret setting the job names, refusing a name that
    is not a setting's or is one of the wrapper's own, so no job can have
    the token written into inputs.json, and a value too short to mask."""
    if not isinstance(names, list):
        raise StepError("the job's secrets are not a list of setting names")
    secrets = {}
    for name in names:
        if (
            not isinstance(name, str)
            or not SETTING.fullmatch(name)
            or name in OWN_SETTINGS
        ):
            raise StepError(f"the job names the secret setting {name!r}")
        value = setting(name)
        if len(value) < SHORTEST_SECRET:
            raise StepError(
                f"the secret setting {name} is shorter than {SHORTEST_SECRET} "
                "characters, too short to mask in what the step prints"
            )
        secrets[name] = value
    return secrets


def setting(name: str) -> str:
    """A setting's value, as Woodpecker hands it to a plugin step."""
    variable = "PLUGIN_" + name.upper()
    if variable not in os.environ:
        raise StepError(f"the secret setting {name} was not given to the step")
    return os.environ[variable]


def say(door: Door, text: str) -> None:
    print(door.masked(text), flush=True)


def under(door: str, url: str) -> bool:
    """Whether `url` is the door or a link under it: the same scheme, host
    and port, its path the door's or below it, with no dot segment and no
    escaped dot, slash or backslash a server might read as one."""
    try:
        base, found = urllib.parse.urlsplit(door), urllib.parse.urlsplit(url)
        places = [
            (part.scheme, part.hostname, part.port or DEFAULT_PORTS.get(part.scheme))
            for part in (base, found)
        ]
    except ValueError:
        return False
    if places[0] != places[1] or found.username is not None or found.password:
        return False
    path = found.path
    if re.search(r"%(2e|2f|5c)|\\", path, re.IGNORECASE) or any(
        segment in (".", "..") for segment in path.split("/")
    ):
        return False
    return path == base.path or path.startswith(base.path.rstrip("/") + "/")


class Refuse(urllib.request.HTTPRedirectHandler):
    """Follow no redirect: a 3xx is an answer the step refuses, so the token
    never goes where the door did not send it and a POST never turns into a
    GET that seems to succeed."""

    def redirect_request(self, *args: Any) -> None:
        return None


class Body:
    """`size` bytes of an open file from its start, read by position, so the
    bytes put are the ones hashed and a retry reads them again."""

    def __init__(self, descriptor: int, size: int) -> None:
        self.descriptor, self.left, self.offset = descriptor, size, 0

    def read(self, wanted: int = -1) -> bytes:
        wanted = self.left if wanted < 0 else min(wanted, self.left)
        chunk = os.pread(self.descriptor, wanted, self.offset)
        self.offset += len(chunk)
        self.left -= len(chunk)
        return chunk


def retried(attempt: Callable[[], Any], what: str = "") -> Any:
    """`attempt`'s answer, made again after each `RetryError` up to `TRIES`."""
    for number in range(TRIES):
        try:
            return attempt()
        except RetryError as error:
            if number == TRIES - 1:
                raise StepError(f"{what}{error}") from None
        time.sleep(BACKOFF * 2**number)
    raise AssertionError("unreachable")


class Door:
    """The step's door at the forge: its link and token, every value the
    wrapper masks, and the one opener every request goes through."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url, token
        self.masks: list[str] = []
        self.pattern = re.compile("(?!)")
        self.add_masks([url, token])
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), Refuse()
        )

    def add_masks(self, values: Any) -> None:
        self.masks = sorted(
            {*self.masks, *(value for value in values if value)}, key=len, reverse=True
        )
        if self.masks:
            self.pattern = re.compile("|".join(map(re.escape, self.masks)))

    def masked(self, text: str) -> str:
        return self.pattern.sub("***", text)

    def attempt(
        self,
        method: str,
        url: str,
        data: bytes | Body | None = None,
        kind: str | None = None,
        checksum: str | None = None,
    ) -> Any:
        """One try at `url`, with the token only when it is the door or a
        link under it, and the SHA-256 `checksum` a PUT link is signed for;
        a `RetryError` for what may pass when tried again."""
        headers = {}
        if checksum is not None:
            headers["x-amz-checksum-sha256"] = checksum
        if under(self.url, url):
            headers["Authorization"] = f"Bearer {self.token}"
        if kind is not None:
            headers["Content-Type"] = kind
        if isinstance(data, Body):
            headers["Content-Length"] = str(data.left)
        try:
            request = urllib.request.Request(url, data, headers, method=method)
            return self.opener.open(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 429 or error.code >= 500:
                raise RetryError(f"{method} answered {error.code}") from None
            raise RefusedError(method, error.code) from None
        except ValueError, http.client.InvalidURL:
            raise StepError(f"{method} was given a link it cannot open") from None
        except (OSError, http.client.HTTPException) as error:
            reason = getattr(error, "reason", error)
            raise RetryError(f"{method} found no answer: {reason}") from None

    def call(
        self,
        method: str,
        url: str,
        document: Any = None,
        ends_token: bool = False,
        exactly: bool = False,
    ) -> Any:
        """Ask the door, sending `document` as JSON, and read its answer:
        none for a 204, which has no body; read `exactly`, each number a
        `Number` as written. A request that `ends_token`,
        done or the end of the pipeline, may have been taken by the door
        though its answer was cut off: tried again, a 401 or 403, the token
        ended, is that request done."""
        data = None if document is None else json.dumps(document).encode()
        tries = 0

        def once() -> bytes | None:
            nonlocal tries
            tries += 1
            kind = None if data is None else JSON
            try:
                answer = self.attempt(method, url, data, kind)
            except RefusedError as error:
                if ends_token and tries > 1 and error.code in (401, 403):
                    return None
                raise
            with answer:
                try:
                    text: bytes = answer.read()
                except (OSError, http.client.HTTPException) as error:
                    raise RetryError(
                        f"{method}'s answer was cut off: {error}"
                    ) from None
            return None if answer.status == 204 else text

        text = retried(once)
        if text is None:
            return None
        if exactly:
            return json.loads(text, parse_float=Number, parse_int=Number)
        return json.loads(text)

    def put(self, url: str, descriptor: int, size: int, path: str, sha256: str) -> None:
        checksum = base64.b64encode(bytes.fromhex(sha256)).decode()

        def once() -> None:
            body = Body(descriptor, size)
            with self.attempt("PUT", url, body, BYTES, checksum) as answer:
                try:
                    answer.read()
                except (OSError, http.client.HTTPException) as error:
                    raise RetryError(f"PUT's answer was cut off: {error}") from None

        retried(once, f"the upload of {path}: ")


def mounted(path: Path) -> bool:
    return os.path.ismount(path)


class Cache:
    """The machine's cache, /cache, the org's folder on the machine's cache
    disk, bind-mounted by the forge, used by steps that run as root: each
    task file once in objects/, named by its SHA-256, with a lock per name
    in locks/ held while it is fetched or linked; each
    program a primitive compiled in programs/<key>/, with the lock
    locks/programs-<key>.lock held while it is compiled or read; each
    environment exec assembled in envs/<fingerprint>-<toolchain>/, with
    the lock locks/envs-<name>.lock held while it is assembled or used,
    made of hard links into store/, whose entries each package is unpacked
    into once, and whose `ready.json` lists the entries it links; the least
    recently used objects, programs and environments removed first past
    the cap, and a store entry with the last folder that lists it; and in
    items/ this running step's folder, with a lock beside it held for the
    step's life, whose item folders hold hard links to the objects each
    item reads and whose .fetching folder holds downloads in progress. /work/cache is a
    link to the step's folder, so the job names a cached file
    cache/<item>/<name> before the folder exists. The lock is made under a
    name of its own, held, and only then named <folder>.lock, and the
    folder made after it, so a step folder never stands without a held
    lock but in a step that is dying."""

    def __init__(self, root: Path) -> None:
        self.root = root
        tidy(root)
        descriptor, made = tempfile.mkstemp(
            prefix="step-", suffix=".lock-new", dir=root / "items"
        )
        os.close(descriptor)
        fresh = Path(made)
        self.held = take_lock(fresh, create=False)
        self.lock = fresh.with_name(fresh.name.removesuffix("-new"))
        fresh.rename(self.lock)
        self.folder = self.lock.with_suffix("")
        self.folder.mkdir()
        self.fetching = self.folder / ".fetching"
        self.fetching.mkdir()
        (WORK / "cache").symlink_to(self.folder)

    @classmethod
    @contextlib.contextmanager
    def for_step(cls, files: list[dict[str, Any]]) -> Iterator[Cache | None]:
        """The cache, for a job with cached files, given back at the step's
        end: its folder removed, then its lock. A step given /cache with no
        cached files, as an exec step that only compiles into programs/,
        has none, but tidies the cache first when it runs as root, so what
        earlier steps compiled is held to the cap."""
        if not any(entry.get("cached") for entry in files):
            if mounted(CACHE) and os.geteuid() == 0:
                tidy(CACHE)
            yield None
            return
        if not mounted(CACHE):
            raise StepError(
                "the job has cached files and the step has no /cache volume"
            )
        cache = cls(CACHE)
        try:
            yield cache
        finally:
            remove(cache.folder)
            cache.lock.unlink(missing_ok=True)
            os.close(cache.held)

    def place(self, door: Door, entry: dict[str, Any], target: Path) -> None:
        """Link the object `entry` names at `target`, fetching it first when
        the cache lacks it. An object already there is hashed again first,
        and removed and fetched anew unless it is still the length and
        SHA-256 its name says: a step's root can write to /cache, so a name
        alone is not trusted."""
        digest = entry["sha256"]
        held = take_lock(self.root / "locks" / digest)
        try:
            found = self.root / "objects" / digest
            if found.is_file() and not holds(found, entry["size"], digest):
                found.unlink()
            if not found.is_file():
                make_room(self.root, entry["size"])
                descriptor, name = tempfile.mkstemp(dir=self.fetching)
                os.close(descriptor)
                try:
                    download(door, entry, Path(name))
                    Path(name).chmod(0o444)
                    Path(name).rename(found)
                finally:
                    Path(name).unlink(missing_ok=True)
            with contextlib.suppress(PermissionError):
                os.utime(found)
            target.parent.mkdir(exist_ok=True)
            os.link(found, target)
        finally:
            os.close(held)


def tidy(root: Path) -> None:
    """Make the cache's folders that are not there yet, sweep it, and bring
    it within its cap, which a primitive's compiled programs, stored
    without the wrapper, may have passed."""
    for name in ("objects", "programs", "envs", "locks", "items"):
        (root / name).mkdir(exist_ok=True)
    sweep(root)
    make_room(root, 0)


def make_room(root: Path, size: int) -> None:
    """Remove the least recently used objects that no item holds and no
    step is fetching or linking, compiled programs no step is compiling or
    reading, and environments no step is assembling or using, with their
    locks, until `size` more bytes fit the cap and the disk has
    `DISK_FREE_SHARE` of its size free. Store entries no folder
    lists go before any of them, and an entry goes with the last folder
    that lists it, in the same call. A download, a compile or an assembly
    in progress is not counted."""
    cap = CACHE_CAP
    if CACHE_CAP_FILE.is_file():
        cap = int(CACHE_CAP_FILE.read_text())
    evicting = take_lock(root / "locks" / "evict")
    try:
        with store_pass(root) as passing:
            store = Store(root, passing)
            found = stored(root, store)
            # An entry no folder lists that this call may not free is
            # left out, so the call does not evict everything else chasing
            # bytes only a later call can free; it may overshoot the cap
            # until then.
            used = store.total - store.held() + sum(entry[1] for entry in found)
            ordered = sorted(found, key=lambda entry: entry[0])
            for _, _, evict in [*store.unlisted(), *ordered]:
                if used + size <= cap and not disk_short(root):
                    break
                used -= evict()
    finally:
        os.close(evicting)


def disk_short(root: Path) -> bool:
    """Whether the disk the cache at `root` is on has less than
    `DISK_FREE_SHARE` of its size free."""
    found = os.statvfs(root)
    return found.f_bavail < DISK_FREE_SHARE * found.f_blocks


@contextlib.contextmanager
def store_pass(root: Path) -> Iterator[bool]:
    """Whether this call may free store entries: only while it holds
    locks/store.lock exclusive, taken without waiting. exec holds it shared
    for the whole of an assembly, since an entry it has just unpacked is
    listed in no `ready.json` yet and would look unused."""
    try:
        held = take_lock(root / "locks" / "store.lock", wait=False)
    except BlockingIOError:
        yield False
        return
    try:
        yield True
    finally:
        os.close(held)


class Store:
    """The store's entries, under /cache/store, as one call of `make_room`
    finds them: each one's size, counted once however many folders link
    it, when it came, and the environment folders whose `ready.json` lists
    it. An entry is freed only by a call that holds the store's lock
    (`passing`), never while a folder lists it, and never younger than
    `STALE_SECONDS`. Its age is its folder's inode change time, which
    unpacking or renaming it into place sets and nothing sets back."""

    def __init__(self, root: Path, passing: bool) -> None:
        self.root = root / "store"
        self.passing = passing
        self.sizes: dict[str, int] = {}
        self.came: dict[str, float] = {}
        self.listing: dict[str, set[Path]] = {}
        for parent in STORE_ENTRIES:
            folder = self.root / parent
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if path.name.startswith(".") or ".evicting-" in path.name:
                    continue
                with contextlib.suppress(FileNotFoundError):
                    seen = path.lstat()
                    if not stat.S_ISDIR(seen.st_mode):
                        continue
                    name = f"{parent}/{path.name}"
                    self.sizes[name] = counted(path, seen)
                    self.came[name] = seen.st_ctime
                    self.listing[name] = set()
        self.total = sum(self.sizes.values())

    def note(self, folder: Path, names: frozenset[str]) -> None:
        """Count each entry of `names` as listed by `folder`."""
        for name in names & self.listing.keys():
            self.listing[name].add(folder)

    def unlisted(self) -> list[tuple[float, int, Callable[[], int]]]:
        """Each entry no folder lists, oldest first, with what frees it."""
        found = [
            (self.came[name], self.sizes[name], functools.partial(self.free, name))
            for name, folders in self.listing.items()
            if not folders
        ]
        return sorted(found, key=lambda entry: entry[0])

    def held(self) -> int:
        """The bytes of the entries no folder lists that this call may not
        free: all of them when the store's lock is held elsewhere, else the
        young ones."""
        return sum(
            self.sizes[name]
            for name, folders in self.listing.items()
            if not folders and not self.freeable(name)
        )

    def freeable(self, name: str) -> bool:
        """Whether this call may free the entry `name`: it holds the store's
        lock, and the entry is no younger than `STALE_SECONDS`."""
        return (
            self.passing
            and name in self.sizes
            and time.time() - self.came[name] >= STALE_SECONDS
        )

    def release(self, folder: Path) -> int:
        """Free each entry whose last listing was `folder`'s, which was just
        evicted, giving back the bytes of each: freed now when this call
        may free it, else left for a later call and out of the count, as
        `held` leaves such entries out."""
        last = []
        for name, folders in self.listing.items():
            if folder in folders:
                folders.discard(folder)
                if not folders:
                    last.append(name)
        return sum(
            self.free(name) if self.freeable(name) else self.sizes[name]
            for name in last
        )

    def free(self, name: str) -> int:
        """Remove the entry `name`, giving back its bytes, or 0 when this
        call may not free it. It leaves its name whole, renamed to
        `<entry>.evicting-<anything>`, before it is removed, so no tool
        takes a half-removed entry for whole; the sweep removes what a
        wrapper killed midway leaves."""
        if not self.freeable(name):
            return 0
        size = self.sizes.pop(name)
        path = self.root / name
        if name.startswith(f"{UV_ARCHIVES}/"):
            self.unpoint(path.name)
        aside = path.with_name(f"{path.name}.evicting-{uuid.uuid4().hex}")
        with contextlib.suppress(FileNotFoundError):
            path.rename(aside)
            remove(aside)
        return size

    def unpoint(self, entry: str) -> None:
        """Remove each pointer uv keeps to its unpacked entry `entry`, a
        `.rev` file under `UV_POINTERS` whose bytes name it, so uv unpacks
        the wheel again rather than trust an entry that is gone."""
        pointers = self.root / UV_POINTERS
        if not pointers.is_dir():
            return
        wanted = entry.encode()
        for pointer in pointers.rglob("*.rev"):
            with contextlib.suppress(OSError):
                descriptor = os.open(
                    pointer, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                )
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        continue
                    content = stream.read(POINTER_LIMIT)
                if wanted in content:
                    pointer.unlink()


def stored(root: Path, store: Store) -> list[tuple[float, int, Callable[[], int]]]:
    """Each object, compiled program and environment in the cache: when it
    was last used, its own size, and what removes it, giving back the bytes
    it freed, 0 when it is in use. An environment was last used when its
    `ready.json` was last touched, which exec does each time it takes the
    folder; its own size leaves out the files it links from the store,
    which `store` counts once, and the entries it lists are counted as its
    in `store`."""
    found: list[tuple[float, int, Callable[[], int]]] = []
    for path in (root / "objects").iterdir():
        seen = path.stat()
        evict = functools.partial(evict_object, root, path, seen)
        found.append((seen.st_mtime, seen.st_size, evict))
    for kind in ("programs", "envs"):
        if not (root / kind).is_dir():
            continue
        for path in (root / kind).iterdir():
            if not NAMES[kind].fullmatch(path.name):
                continue
            with contextlib.suppress(FileNotFoundError):
                seen = path.lstat()
                if not stat.S_ISDIR(seen.st_mode):
                    continue
                if kind == "programs":
                    size = counted(path, seen)
                    evict = functools.partial(evict_program, root, path, size)
                    found.append((seen.st_mtime, size, evict))
                    continue
                used, names = ready_of(path)
                store.note(path, names)
                size = counted(path, seen, linked=False)
                evict = functools.partial(evict_environment, root, path, size, store)
                found.append((used, size, evict))
    return found


def ready_of(folder: Path) -> tuple[float, frozenset[str]]:
    """When the environment at `folder` was last used, its `ready.json`'s
    mtime, and the store entries that file lists; for a folder without a
    `ready.json` exec wrote, the oldest time and no entries."""
    try:
        descriptor = os.open(
            folder / "ready.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except OSError:
        return 0.0, frozenset()
    with os.fdopen(descriptor, "rb") as stream:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode):
            return 0.0, frozenset()
        text = stream.read(READY_LIMIT + 1)
    try:
        document = json.loads(text) if len(text) <= READY_LIMIT else None
    except ValueError:
        document = None
    entries = document.get("store") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return found.st_mtime, frozenset()
    return found.st_mtime, frozenset(name for name in entries if isinstance(name, str))


def counted(path: Path, seen: os.stat_result, linked: bool = True) -> int:
    """The size of the folder at `path`, listed as `seen`, by `folder_size`,
    walked once a step."""
    known = str(path), seen.st_dev, seen.st_ino
    if known not in FOLDER_SIZES:
        FOLDER_SIZES[known] = folder_size(path, linked=linked)
    return FOLDER_SIZES[known]


def evict_object(root: Path, path: Path, seen: os.stat_result) -> int:
    """Remove the object at `path`, listed as `seen`, and its lock, unless
    an item holds it or a step is fetching or linking it."""
    if seen.st_nlink > 1:
        return 0
    lock = root / "locks" / path.name
    try:
        held = take_lock(lock, wait=False)
    except BlockingIOError:
        return 0
    try:
        # Linked since it was listed, or removed, by a step that held the
        # lock in between.
        now = path.lstat()
        if now.st_nlink > 1:
            return 0
        path.unlink()
        lock.unlink(missing_ok=True)
        return now.st_size
    except FileNotFoundError:
        return seen.st_size
    finally:
        os.close(held)


def evict_program(root: Path, path: Path, size: int) -> int:
    """Remove the compiled program's folder at `path`, of `size` bytes, as
    `removed` does."""
    return size if removed(root, path) else 0


def evict_environment(root: Path, path: Path, size: int, store: Store) -> int:
    """Remove the environment's folder at `path`, of `size` bytes of its
    own, as `removed` does, and with it each store entry it held the last
    listing of, giving back their bytes with its own."""
    if not removed(root, path):
        return 0
    return size + store.release(path)


def removed(root: Path, path: Path) -> bool:
    """Remove the compiled program's or environment's folder at `path`, and
    its lock, unless a step is compiling, assembling or using it: whether
    it is gone. The folder leaves its name whole, renamed to
    `<name>.evicting-<anything>`, before it is removed, so a wrapper killed
    midway leaves no half-removed folder that exec would take for whole;
    the sweep removes what it leaves."""
    lock = root / "locks" / f"{path.parent.name}-{path.name}.lock"
    try:
        held = take_lock(lock, wait=False)
    except BlockingIOError:
        return False
    try:
        aside = path.with_name(f"{path.name}.evicting-{uuid.uuid4().hex}")
        try:
            path.rename(aside)
        except FileNotFoundError:
            return True
        remove(aside)
        lock.unlink(missing_ok=True)
        return True
    finally:
        os.close(held)


def folder_size(folder: Path, linked: bool = True) -> int:
    """The bytes of a folder's plain files, nested folders included, each
    inode counted once. With `linked` false, an inode that also has links
    outside the folder is left out: an environment's file linked from the
    store, which the store counts."""
    sizes: dict[tuple[int, int], tuple[int, int]] = {}
    inside: dict[tuple[int, int], int] = {}
    for parent, directories, files in os.walk(folder, followlinks=False):
        directories[:] = [
            name for name in directories if not (Path(parent) / name).is_symlink()
        ]
        for name in files:
            with contextlib.suppress(FileNotFoundError):
                found = (Path(parent) / name).lstat()
                key = found.st_dev, found.st_ino
                if stat.S_ISREG(found.st_mode):
                    sizes[key] = found.st_size, found.st_nlink
                    inside[key] = inside.get(key, 0) + 1
    return sum(
        size for key, (size, links) in sizes.items() if linked or inside[key] >= links
    )


def holds(path: Path, size: int, digest: str) -> bool:
    """Whether the plain file at `path` is `size` bytes with SHA-256 `digest`."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        found = os.fstat(descriptor)
        return (
            stat.S_ISREG(found.st_mode)
            and found.st_size == size
            and digest_of(descriptor, size) == digest
        )
    finally:
        os.close(descriptor)


def take_lock(path: Path, wait: bool = True, create: bool = True) -> int:
    """An flock on `path`, held until the descriptor it returns is closed.
    The lock holds only while `path` still names the file locked, so one
    removed by a holder, as a sweep removes a lock, is taken again under
    its new file."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    while True:
        held = os.open(path, flags, 0o444)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            with contextlib.suppress(FileNotFoundError):
                if os.path.samestat(os.fstat(held), os.lstat(path)):
                    return held
        except BaseException:
            os.close(held)
            raise
        os.close(held)
        if not create:
            raise FileNotFoundError(path)


def sweep(root: Path) -> None:
    """Remove what killed steps left: each step folder whose lock no one
    holds, whose links would keep objects past the cap and whose .fetching
    folder holds a part of a download; a lock still being made, a step
    folder without one, or a folder in programs/ or envs/ a program was
    being compiled or an environment assembled into, `<name>.<anything>`,
    whose lock no one holds, once older than `STALE_SECONDS`, and anything
    else there not under a name of its kind; each store entry a wrapper
    was removing, `<entry>.evicting-<anything>`; what a killed step left
    half written in the store, once older than `STALE_SECONDS`, while no
    step holds the store's lock (`unfinished`); and the lock of every name
    no object has and no step holds."""
    items = root / "items"
    for lock in items.glob("*.lock"):
        try:
            held = take_lock(lock, wait=False, create=False)
        except BlockingIOError, FileNotFoundError:
            continue
        remove(items / lock.stem)
        lock.unlink(missing_ok=True)
        os.close(held)
    now = time.time()
    for path in items.iterdir():
        with contextlib.suppress(FileNotFoundError):
            if path.suffix == ".lock" or now - path.lstat().st_mtime < STALE_SECONDS:
                continue
            if path.name.endswith(".lock-new"):
                with contextlib.suppress(BlockingIOError, FileNotFoundError):
                    held = take_lock(path, wait=False, create=False)
                    path.unlink(missing_ok=True)
                    os.close(held)
            elif path.is_dir() and not path.with_name(path.name + ".lock").exists():
                remove(path)
    for kind, named in NAMES.items():
        if not (root / kind).is_dir():
            continue
        for path in (root / kind).iterdir():
            with contextlib.suppress(FileNotFoundError):
                if (
                    named.fullmatch(path.name)
                    or now - path.lstat().st_mtime < STALE_SECONDS
                ):
                    continue
                key = path.name.partition(".")[0]
                if not named.fullmatch(key):
                    discard(path)
                    continue
                with contextlib.suppress(BlockingIOError):
                    held = take_lock(root / "locks" / f"{kind}-{key}.lock", wait=False)
                    try:
                        discard(path)
                    finally:
                        os.close(held)
    for parent in STORE_ENTRIES:
        folder = root / "store" / parent
        if folder.is_dir():
            for path in folder.iterdir():
                if ".evicting-" in path.name and not path.name.startswith("."):
                    discard(path)
    with store_pass(root) as passing:
        if passing:
            for path in unfinished(root / "store"):
                with contextlib.suppress(FileNotFoundError):
                    if now - path.lstat().st_mtime >= STALE_SECONDS:
                        discard(path)

    for lock in (root / "locks").iterdir():
        if (
            lock.name in ("evict", "store.lock")
            or (root / "objects" / lock.name).exists()
        ):
            continue
        with contextlib.suppress(BlockingIOError, FileNotFoundError):
            held = take_lock(lock, wait=False, create=False)
            if not (root / "objects" / lock.name).exists():
                lock.unlink(missing_ok=True)
            os.close(held)


def unfinished(store: Path) -> Iterator[Path]:
    """Each file or folder a killed step left half written in the store at
    `store` (`UNFINISHED`). Only a call that holds the store's lock
    exclusive may take them for left, since every write to the store is
    made while exec holds it shared."""
    for parent, named in UNFINISHED.items():
        folder = store / parent
        if folder.is_dir():
            yield from (path for path in folder.iterdir() if named.fullmatch(path.name))
    wheels = store / "wheels"
    if wheels.is_dir():
        for entry in wheels.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                yield from (
                    path for path in entry.iterdir() if path.name.startswith(".")
                )


def discard(path: Path) -> None:
    """Remove `path`, a folder as `remove` does, or any other file."""
    if path.is_dir() and not path.is_symlink():
        remove(path)
    else:
        path.unlink(missing_ok=True)


def remove(folder: Path) -> None:
    """Remove a step folder, whose item folders are read-only, or a compiled
    program's."""
    for parent, _, _ in os.walk(folder):
        with contextlib.suppress(OSError):
            Path(parent).chmod(0o700)
    shutil.rmtree(folder, ignore_errors=True)


def place(door: Door, files: list[dict[str, Any]], cache: Cache | None) -> None:
    """Put every file of the job where its path says, fetching side by side."""
    (WORK / "in").mkdir(exist_ok=True)
    fetches: list[Callable[[], None]] = []
    for entry in files:
        parts = entry["path"].rstrip("/").split("/")
        cached = bool(entry.get("cached"))
        if (
            len(parts) < 2
            or not all(NAME.fullmatch(part) for part in parts)
            or parts[0] != ("cache" if cached else "in")
            or (cached and (len(parts) != 3 or "workspace" in entry))
            or (entry.get("folder") and (cached or set(entry) != {"path", "folder"}))
        ):
            raise StepError(f"the job places a file at {entry['path']!r}")
        if entry.get("folder"):
            WORK.joinpath(*parts).mkdir(parents=True)
        elif "workspace" in entry:
            from_workspace(entry["workspace"], WORK.joinpath(*parts))
        elif cache is not None and cached:
            target = cache.folder.joinpath(*parts[1:])
            fetches.append(functools.partial(cache.place, door, entry, target))
        else:
            target = WORK.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            fetches.append(functools.partial(download, door, entry, target))
    with concurrent.futures.ThreadPoolExecutor(PARALLEL) as pool:
        for fetched in [pool.submit(fetch) for fetch in fetches]:
            fetched.result()
    if cache is not None:
        remove(cache.fetching)
        for item in cache.folder.iterdir():
            item.chmod(0o555)


def download(door: Door, entry: dict[str, Any], target: Path) -> None:
    """Fetch one file to `target`, refusing it unless it is exactly the
    length and SHA-256 the job names, and fetching it again from its start
    when the answer is cut off or does not come. An error names the file's
    path, never its link."""
    path, size = entry["path"], entry["size"]
    wrong = f"the file for {path} is not the length and SHA-256 the job names"

    def written(write: Callable[..., object], *args: Any) -> None:
        """A write to the file, failing the step as one, never retried."""
        try:
            write(*args)
        except OSError as error:
            reason = error.strerror or error
            raise StepError(f"could not write the file for {path}: {reason}") from None

    def once(out: Any) -> None:
        try:
            answer = door.attempt("GET", entry["url"])
        except StepError as error:
            raise StepError(f"the file for {path}: {error}") from None
        digest, got = hashlib.sha256(), 0
        with answer:
            written(out.seek, 0)
            written(out.truncate)
            while True:
                try:
                    chunk = answer.read(CHUNK)
                except (OSError, http.client.HTTPException) as error:
                    raise RetryError(f"the answer was cut off: {error}") from None
                if not chunk:
                    break
                got += len(chunk)
                if got > size:
                    raise StepError(wrong)
                digest.update(chunk)
                written(out.write, chunk)
        if got < size:
            raise RetryError(f"the answer ended after {got} of {size} bytes")
        if digest.hexdigest() != entry["sha256"]:
            raise StepError(wrong)

    try:
        try:
            out = target.open("wb")
        except OSError as error:
            reason = error.strerror or error
            raise StepError(f"could not write the file for {path}: {reason}") from None
        with out:
            retried(functools.partial(once, out), f"the file for {path}: ")
            written(out.flush)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def from_workspace(source: str, target: Path) -> None:
    """Copy a file or folder of the step's workspace, the submission the clone
    step checked out, to `target`, refusing a link or anything else not a
    plain file or folder: the wrapper may be root, the content the
    entrant's."""
    workspace = Path(os.environ["CI_WORKSPACE"]).resolve()
    found = (workspace / source).resolve()
    if not found.is_relative_to(workspace) or not found.exists():
        raise StepError(f"the workspace has no {source!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if found.is_file():
        shutil.copyfile(found, target)
        return
    for parent, folders, names in os.walk(found):
        copy = target / Path(parent).relative_to(found)
        copy.mkdir()
        for name in folders + names:
            path = Path(parent) / name
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise StepError(f"the workspace's {source!r} holds {name!r}")
            if path.is_file():
                shutil.copyfile(path, copy / name)


def fill_secrets(value: Any, secrets: dict[str, str]) -> Any:
    """`value` with each `{"secret": <name>}` replaced by that secret's value."""
    if isinstance(value, dict):
        if set(value) == {"secret"}:
            if value["secret"] not in secrets:
                raise StepError(f"the inputs name the secret {value['secret']}")
            return secrets[value["secret"]]
        return {key: fill_secrets(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [fill_secrets(item, secrets) for item in value]
    return value


def run(door: Door, command: list[str], job: dict[str, Any]) -> str:
    """Run the primitive in /work, what it prints masked into the step's log,
    and kill it and all it started at the job's time limit; as the
    container's first process, kill whatever is left once it ends, so
    nothing changes /work/out while it is uploaded. Says how it ended."""
    if not command:
        raise StepError("the image names no primitive to run")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PLUGIN_")}
    if "box_users" in job:
        users = job["box_users"]
        environment["UNICON_BOX_USERS"] = f"{users['first']}-{users['last']}"
        environment["UNICON_STEP_MEMORY_MB"] = str(job["step_memory_mb"])
    process = subprocess.Popen(
        command,
        cwd=WORK,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    printed = process.stdout
    assert printed is not None
    relaying = threading.Thread(target=relay, args=(door, printed), daemon=True)
    relaying.start()
    try:
        code = process.wait(timeout=job["time_limit"])
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise StepError(
            "the primitive was still running at the step's time limit of "
            f"{job['time_limit']} s, and was killed"
        ) from None
    finally:
        kill_the_rest()
        relaying.join(timeout=10)
    return f"was killed by signal {-code}" if code < 0 else f"exited with code {code}"


def kill_the_rest() -> None:
    """As the container's first process, kill every other process in it and
    reap them. Elsewhere, in the tests, there is no such set to kill."""
    if os.getpid() != 1:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(-1, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if os.waitpid(-1, os.WNOHANG) == (0, 0):
                time.sleep(0.05)
        except ChildProcessError:
            return


def relay(door: Door, printed: Any) -> None:
    """Copy what the primitive prints into the step's log, masked. A line
    longer than one read is written in parts, each but the last keeping
    back as many characters as the longest mask has, less one, so no mask
    is split across two parts."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    keep = max(map(len, door.masks), default=1) - 1
    carried = ""
    while piece := printed.readline(1 << 16):
        text = carried + decoder.decode(piece)
        if text.endswith("\n"):
            write(door.masked(text[:-1]) + "\n")
            carried = ""
            continue
        cut = max(0, len(text) - keep)
        for found in door.pattern.finditer(text):
            if found.start() < cut < found.end():
                cut = found.end()
        write(door.masked(text[:cut]))
        carried = text[cut:]
    text = carried + decoder.decode(b"", final=True)
    if text:
        write(door.masked(text) + "\n")


def write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def read_plain(path: Path, ended: str) -> str:
    """A file the primitive wrote, refused unless it is a plain file; its
    absence told with how the primitive `ended`, such as its exit code."""
    name = path.relative_to(WORK)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise StepError(f"the primitive {ended} and wrote no {name}") from None
    except OSError:
        raise StepError(f"the primitive's {name} is not a plain file") from None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise StepError(f"the primitive's {name} is not a plain file")
        return stream.read().decode("utf-8")


def write_outputs(text: str) -> None:
    """Write outputs.json anew, never through a link the primitive left."""
    (WORK / "outputs.json").unlink(missing_ok=True)
    (WORK / "outputs.json").write_text(text, encoding="utf-8")


def mask(door: Door, text: str) -> dict[str, Any]:
    """outputs.json with every secret, the link and the token masked in its
    texts, written back only when one was, each number as written."""
    document = json.loads(text, parse_float=Number, parse_int=Number)
    if not isinstance(document, dict):
        raise StepError("the primitive's outputs.json is not a JSON object")
    masked: dict[str, Any] = masked_value(door, document)
    if (written := exact(masked)) != exact(document):
        write_outputs(written)
    return masked


def masked_value(door: Door, value: Any, key: object = None) -> Any:
    """`value` masked: each text, but an `outcome`, which is a word of the
    contract's, and each number that holds a masked value, which becomes
    the text `***`, so it can pass as no number."""
    if isinstance(value, Number):
        return "***" if door.masked(value) != value else value
    if isinstance(value, str):
        return value if key == "outcome" else door.masked(value)
    if isinstance(value, dict):
        return {name: masked_value(door, item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [masked_value(door, item) for item in value]
    return value


def exact(value: Any) -> str:
    """`value` as JSON, each number as it was read."""
    if isinstance(value, Number):
        return str.__str__(value)
    if isinstance(value, dict):
        pairs = (f"{exact(key)}:{exact(item)}" for key, item in value.items())
        return "{" + ",".join(pairs) + "}"
    if isinstance(value, list):
        return "[" + ",".join(exact(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def upload(door: Door, job: dict[str, Any]) -> None:
    """Upload outputs.json and every plain file under /work/out by the links
    the door gives for them, side by side, each hashed, measured and put
    from one descriptor, opened without following a link."""
    paths = [WORK / "outputs.json"]
    for parent, _, names in os.walk(WORK / "out"):
        paths += [Path(parent) / name for name in names]
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    opened: list[tuple[str, int, int]] = []
    try:
        for path in paths:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                descriptor = os.open(path, flags)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOENT, errno.ENXIO):
                    continue
                raise StepError(
                    f"could not open {path.relative_to(WORK)}: {error}"
                ) from None
            found = os.fstat(descriptor)
            if not stat.S_ISREG(found.st_mode):
                os.close(descriptor)
                continue
            opened.append((str(path.relative_to(WORK)), descriptor, found.st_size))
        digests = [digest_of(descriptor, size) for _, descriptor, size in opened]
        listing = [
            {"path": name, "sha256": digest, "size": size}
            for (name, _, size), digest in zip(opened, digests, strict=True)
        ]
        links = door.call("POST", job["uploads"], {"files": listing})["files"]
        with concurrent.futures.ThreadPoolExecutor(PARALLEL) as pool:
            puts = [
                pool.submit(door.put, links[name], descriptor, size, name, digest)
                for (name, descriptor, size), digest in zip(
                    opened, digests, strict=True
                )
            ]
            for put in puts:
                put.result()
    finally:
        for _, descriptor, _ in opened:
            os.close(descriptor)


def digest_of(descriptor: int, size: int) -> str:
    digest, body = hashlib.sha256(), Body(descriptor, size)
    while chunk := body.read(CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
