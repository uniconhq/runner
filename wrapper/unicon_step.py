#!/usr/local/bin/python3 -I
"""The step wrapper, `unicon-step`: the entrypoint of every primitive image
built on ghcr.io/uniconhq/primitive-base, and all a Woodpecker step does
besides the primitive's own program, which is the image's command.

A grading step is a plugin step with two settings: `data_url`, the step's
door at the forge, and `data_token`, which goes only in an Authorization
header and only to the door and the links under it. In order, the wrapper:

1. makes itself non-dumpable, so no process of its user reads its
   environment through /proc/1/environ, nor its memory;
2. fetches the step's job (schemas/job.schema.json);
3. places the job's files, each checked against its length and SHA-256:
   into /work/in, or a cached one through the machine's cache at /cache,
   linked into an item folder of this running step's own;
4. writes /work/inputs.json with each secret's value filled in, and runs
   the primitive with no PLUGIN_ variable in its environment and its box
   users in UNICON_BOX_USERS, killing it at the job's time limit;
5. masks every secret, the link and the token in what the primitive wrote
   and printed, uploads /work/outputs.json and /work/out, and tells the
   door the step is done;
6. exits 1 when the step did not do its work, or when the job says a
   non-accepted outcome fails the step and one is not accepted.

Whatever stops the step once the job came is written as the error of
outputs.json and uploaded, so the forge reads one place. Exit 2 is the
wrapper not reaching the forge. With the setting `mode: report` it only
tells the door the pipeline ended. With no data link it is not in a
grading step and runs the command in its place, as the primitives' own
tests start an image.
"""

import concurrent.futures
import contextlib
import ctypes
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

JOB_VERSION = 1
WORK = Path("/work")
CACHE = Path("/cache")
CACHE_CAP_FILE = Path("/etc/unicon/cache-cap")
"""Where a machine may set its cache's cap in bytes; else `CACHE_CAP`."""
CACHE_CAP = 10 * 1024**3
PARALLEL = 8
TRIES = 4
BACKOFF = 1.0
"""Seconds before the first retry, doubled for each after."""
CHUNK = 1 << 20
TIMEOUT = 60
PR_SET_DUMPABLE = 4
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}")


class StepError(Exception):
    """What kept the step from doing its work, a sentence for staff."""


class Number(str):
    """A JSON number as the primitive wrote it, so it is written back so."""


def main(argv: list[str]) -> int:
    url = os.environ.get("PLUGIN_DATA_URL")
    if not url:
        if len(argv) < 2:
            print("unicon-step: no data link and no command", file=sys.stderr)
            return 2
        os.execvp(argv[1], argv[1:])
    undumpable()
    door = Door(url, os.environ.get("PLUGIN_DATA_TOKEN", ""))
    try:
        if not door.token:
            raise StepError("the step has a data link and no data token")
        if os.environ.get("PLUGIN_MODE") == "report":
            door.call("POST", url, b"{}")
            say(door, "unicon-step: told the forge the pipeline ended")
            return 0
        job = door.call("GET", url)
        if not isinstance(job, dict) or job.get("schema_version") != JOB_VERSION:
            raise StepError(f"the job is not written against version {JOB_VERSION}")
        return grade(door, job, argv[1:])
    except (StepError, OSError, ValueError, KeyError, TypeError) as error:
        say(door, f"unicon-step: {type(error).__name__}: {error}")
        return 2


def grade(door: Door, job: dict[str, Any], command: list[str]) -> int:
    """Steps 3 to 6 for one job, and the exit code."""
    outputs = WORK / "outputs.json"
    try:
        secrets = {name: setting(name) for name in job.get("secrets", [])}
        door.masks += [value for value in secrets.values() if value]
        with Cache.for_step(job["files"]) as cache:
            place(door, job["files"], cache)
            filled = fill_secrets(job["inputs"], secrets)
            (WORK / "inputs.json").write_text(json.dumps(filled, ensure_ascii=False))
            run(door, command, job)
        if not outputs.is_file():
            raise StepError("the primitive ended and wrote no outputs.json")
        document = mask(door, outputs.read_text(encoding="utf-8"))
    except (StepError, OSError, ValueError) as error:
        version = job["inputs"].get("schema_version")
        document = {"schema_version": version, "error": door.masked(str(error))}
        outputs.write_text(json.dumps(document, ensure_ascii=False))
    upload(door, job)
    door.call("POST", job["done"], b"{}")
    if "error" in document:
        say(door, f"unicon-step: the step failed: {document['error']}")
        return 1
    found = [document.get("outputs", {})]
    found += [item.get("outputs", {}) for item in document.get("batch", [])]
    refused = [
        o["outcome"] for o in found if o.get("outcome", "accepted") != "accepted"
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


def setting(name: str) -> str:
    """A setting's value, as Woodpecker hands it to a plugin step."""
    variable = "PLUGIN_" + name.upper().replace("-", "_").replace(".", "_")
    if variable not in os.environ:
        raise StepError(f"the secret setting {name} was not given to the step")
    return os.environ[variable]


def say(door: Door, text: str) -> None:
    print(door.masked(text), flush=True)


class Door:
    """The step's door at the forge: its link and token, and every value the
    wrapper masks."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url, token
        self.masks = [value for value in (url, token) if value]

    def masked(self, text: str) -> str:
        for value in sorted(self.masks, key=len, reverse=True):
            text = text.replace(value, "***")
        return text

    def request(self, method: str, url: str, data: bytes | Path | None = None) -> Any:
        """Open `url`, with the token only when it is the door or under it,
        retrying what may pass: no answer, a 429 or a 5xx. A file is sent
        as it is read."""
        headers = {}
        if url == self.url or url.startswith(self.url.rstrip("/") + "/"):
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        for attempt in range(TRIES):
            try:
                with contextlib.ExitStack() as stack:
                    body: Any = data
                    if isinstance(data, Path):
                        body = stack.enter_context(data.open("rb"))
                        headers["Content-Length"] = str(data.stat().st_size)
                    request = urllib.request.Request(url, body, headers, method=method)
                    return urllib.request.urlopen(request, timeout=TIMEOUT)
            except urllib.error.HTTPError as error:
                if (error.code != 429 and error.code < 500) or attempt == TRIES - 1:
                    raise StepError(f"{method} answered {error.code}") from None
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == TRIES - 1:
                    raise StepError(f"{method} found no answer: {error}") from None
            time.sleep(BACKOFF * 2**attempt)
        raise AssertionError("unreachable")

    def call(self, method: str, url: str, data: bytes | None = None) -> Any:
        with self.request(method, url, data) as answer:
            text = answer.read()
        return json.loads(text) if text.strip() else None


class Cache:
    """The machine's cache, /cache, one Docker volume per org: each task file
    once in objects/, named by its SHA-256, with a lock per name in locks/
    held while it is fetched or linked, the least recently used removed
    first past the cap; and in items/ this running step's folder, made by
    mkdtemp with a lock beside it held for the step's life, whose item
    folders hold hard links to the objects each item reads. /work/cache is
    a link to the step's folder, so the job names a cached file
    cache/<item>/<name> before the folder exists."""

    def __init__(self, root: Path) -> None:
        self.root = root
        for name in ("objects", "locks", "tmp", "items"):
            (root / name).mkdir(exist_ok=True)
            with contextlib.suppress(PermissionError):
                (root / name).chmod(0o1777)
        sweep(root / "items")
        self.folder = Path(tempfile.mkdtemp(prefix="step-", dir=root / "items"))
        self.lock = self.folder.with_name(self.folder.name + ".lock")
        fresh = self.folder.with_name(self.folder.name + ".lock-new")
        self.held = take_lock(fresh)
        fresh.rename(self.lock)
        (WORK / "cache").symlink_to(self.folder)

    @classmethod
    @contextlib.contextmanager
    def for_step(cls, files: list[dict[str, Any]]) -> Iterator[Cache | None]:
        """The cache, for a job with cached files, given back at the step's
        end: its folder removed, then its lock."""
        if not any(entry.get("cached") for entry in files):
            yield None
            return
        cache = cls(CACHE)
        try:
            yield cache
        finally:
            remove(cache.folder)
            cache.lock.unlink(missing_ok=True)
            os.close(cache.held)

    def place(self, door: Door, entry: dict[str, Any], target: Path) -> None:
        """Link the object `entry` names at `target`, fetching it first when
        the cache lacks it."""
        digest = entry["sha256"]
        held = take_lock(self.root / "locks" / digest)
        try:
            found = self.root / "objects" / digest
            if not found.is_file():
                self.make_room(entry["size"])
                descriptor, name = tempfile.mkstemp(dir=self.root / "tmp")
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

    def make_room(self, size: int) -> None:
        """Remove the least recently used objects that no item holds and no
        step is fetching or linking, until `size` more bytes fit the cap."""
        cap = CACHE_CAP
        if CACHE_CAP_FILE.is_file():
            cap = int(CACHE_CAP_FILE.read_text())
        evicting = take_lock(self.root / "locks" / "evict")
        try:
            found = [(path, path.stat()) for path in (self.root / "objects").iterdir()]
            used = sum(stat.st_size for _, stat in found)
            for path, stat in sorted(found, key=lambda pair: pair[1].st_mtime):
                if used + size <= cap:
                    break
                if stat.st_nlink > 1:
                    continue
                with contextlib.suppress(BlockingIOError):
                    held = take_lock(self.root / "locks" / path.name, wait=False)
                    path.unlink(missing_ok=True)
                    os.close(held)
                    used -= stat.st_size
        finally:
            os.close(evicting)


def take_lock(path: Path, wait: bool = True, create: bool = True) -> int:
    """An flock on `path`, held until the descriptor it returns is closed."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    held = os.open(path, flags, 0o444)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BaseException:
        os.close(held)
        raise
    return held


def sweep(items: Path) -> None:
    """Remove every step folder whose lock no one holds: what a step killed
    before its end left, whose links would keep objects past the cap. A
    lock is taken under its final name only once held, so one being made
    is never swept."""
    for lock in items.glob("*.lock"):
        try:
            held = take_lock(lock, wait=False, create=False)
        except BlockingIOError, FileNotFoundError:
            continue
        remove(items / lock.stem)
        lock.unlink(missing_ok=True)
        os.close(held)


def remove(folder: Path) -> None:
    """Remove a step folder, whose item folders are read-only."""
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
        ):
            raise StepError(f"the job places a file at {entry['path']!r}")
        if "workspace" in entry:
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
        for item in cache.folder.iterdir():
            item.chmod(0o555)


def download(door: Door, entry: dict[str, Any], target: Path) -> None:
    """Fetch one file to `target`, refusing it unless it is exactly the
    length and SHA-256 the job names."""
    digest, size = hashlib.sha256(), 0
    with door.request("GET", entry["url"]) as answer, target.open("wb") as out:
        while chunk := answer.read(CHUNK):
            size += len(chunk)
            if size > entry["size"]:
                break
            digest.update(chunk)
            out.write(chunk)
    if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
        target.unlink()
        raise StepError(
            f"the file for {entry['path']} is not the length and SHA-256 the job names"
        )


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


def run(door: Door, command: list[str], job: dict[str, Any]) -> None:
    """Run the primitive in /work, what it prints masked into the step's log,
    and kill it and all it started at the job's time limit."""
    if not command:
        raise StepError("the image names no primitive to run")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PLUGIN_")}
    if "box_users" in job:
        users = job["box_users"]
        environment["UNICON_BOX_USERS"] = f"{users['first']}-{users['last']}"
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

    def relay() -> None:
        for line in iter(lambda: printed.readline(1 << 16), b""):
            say(door, line.decode("utf-8", "replace").rstrip("\n"))

    relaying = threading.Thread(target=relay, daemon=True)
    relaying.start()
    try:
        process.wait(timeout=job["time_limit"])
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise StepError(
            "the primitive was still running at the step's time limit of "
            f"{job['time_limit']} s, and was killed"
        ) from None
    finally:
        relaying.join(timeout=10)


def mask(door: Door, text: str) -> dict[str, Any]:
    """outputs.json with every secret, the link and the token masked in its
    texts, written back only when one was, each number as written."""
    document = json.loads(text, parse_float=Number, parse_int=Number)
    if not isinstance(document, dict):
        raise StepError("the primitive's outputs.json is not a JSON object")
    masked: dict[str, Any] = masked_value(door, document)
    if (written := exact(masked)) != exact(document):
        (WORK / "outputs.json").write_text(written, encoding="utf-8")
    return masked


def masked_value(door: Door, value: Any) -> Any:
    if isinstance(value, str) and not isinstance(value, Number):
        return door.masked(value)
    if isinstance(value, dict):
        return {key: masked_value(door, item) for key, item in value.items()}
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
    the door gives for them, side by side."""
    paths = [WORK / "outputs.json"]
    for parent, _, names in os.walk(WORK / "out"):
        paths += [Path(parent) / name for name in names]
    listing: list[dict[str, Any]] = [
        {
            "path": str(p.relative_to(WORK)),
            "sha256": sha256(p),
            "size": p.stat().st_size,
        }
        for p in paths
        if p.is_file() and not p.is_symlink()
    ]
    asked = json.dumps({"files": listing}).encode()
    links = door.call("POST", job["uploads"], asked)["files"]
    with concurrent.futures.ThreadPoolExecutor(PARALLEL) as pool:
        puts = [
            pool.submit(door.request, "PUT", links[entry["path"]], WORK / entry["path"])
            for entry in listing
        ]
        for put in puts:
            put.result().close()


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
