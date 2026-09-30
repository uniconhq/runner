"""What the filter lets through, as pure functions of the request, the caller
and the settings.

Passed: ping and version; creating a container that is a grading step; and
start, wait, kill, stop, inspect, logs and delete on a container this filter
created for the caller's own grading, plus inspecting the caller's own
container, which is how a harness learns its workspace volume. Everything
else is refused with a reason.

A create is judged on its whole body, not its address. The daemon's JSON
decoder matches field names without regard to case, so a check that looked
for `Privileged` would miss `privileged`; the body is therefore read against
the exact list of fields the Engine API knows, any other spelling is refused,
and every field the filter does not expect to carry a value must carry none.
Below the fields it reads by name, where it only asks whether a structure is
empty, it compares field names without regard to case, as the daemon does.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from unicon_filter.config import Config

GRADING_LABEL = "unicon.grading"
TIME_LABEL = "unicon.time_ms"
STEP_LABEL = "unicon.step"
HARNESS_LABEL = "unicon.harness"
"""The harness container a step belongs to. The filter writes it on every create
and nothing else may set it, so a restarted filter knows each step's harness.
"""

LABELS = frozenset({GRADING_LABEL, TIME_LABEL, STEP_LABEL})
SECURITY_OPTIONS = frozenset({"no-new-privileges", "seccomp=builtin"})
WORK_TARGET = "/work"
TMP_TARGET = "/tmp"
SUBPATH = re.compile(r"unicon-steps/[A-Za-z0-9][A-Za-z0-9._-]*")
USER = re.compile(r"([0-9]{1,10}):([0-9]{1,10})")
CONTAINER = re.compile(r"[0-9a-f]{64}")
CLOCK = re.compile(r"[0-9]{1,10}")
STEP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
VERSIONED = re.compile(r"(?:/v1\.[0-9]{1,3})?(/.*)")
BOOLEAN = frozenset({"0", "1", "true", "false"})

CONFIG_FIELDS = frozenset(
    """
    Hostname Domainname User AttachStdin AttachStdout AttachStderr ExposedPorts Tty
    OpenStdin StdinOnce Env Cmd Healthcheck ArgsEscaped Image Volumes WorkingDir
    Entrypoint NetworkDisabled MacAddress OnBuild Labels StopSignal StopTimeout
    Shell HostConfig NetworkingConfig
    """.split()
)
HOST_FIELDS = frozenset(
    """
    CpuShares Memory CgroupParent BlkioWeight BlkioWeightDevice BlkioDeviceReadBps
    BlkioDeviceWriteBps BlkioDeviceReadIOps BlkioDeviceWriteIOps CpuPeriod CpuQuota
    CpuRealtimePeriod CpuRealtimeRuntime CpusetCpus CpusetMems Devices
    DeviceCgroupRules DeviceRequests KernelMemory KernelMemoryTCP MemoryReservation
    MemorySwap MemorySwappiness NanoCpus OomKillDisable Init PidsLimit Ulimits
    CpuCount CpuPercent IOMaximumIOps IOMaximumBandwidth Binds ContainerIDFile
    LogConfig NetworkMode PortBindings RestartPolicy AutoRemove VolumeDriver
    VolumesFrom Mounts ConsoleSize Annotations CapAdd CapDrop CgroupnsMode Dns
    DnsOptions DnsSearch ExtraHosts GroupAdd IpcMode Cgroup Links OomScoreAdj
    PidMode Privileged PublishAllPorts ReadonlyRootfs SecurityOpt StorageOpt Tmpfs
    UTSMode UsernsMode ShmSize Sysctls Runtime Isolation MaskedPaths ReadonlyPaths
    Capabilities
    """.split()
)
MOUNT_FIELDS = frozenset(
    """
    Type Source Target ReadOnly Consistency BindOptions VolumeOptions TmpfsOptions
    ImageOptions ClusterOptions
    """.split()
)
VOLUME_OPTION_FIELDS = frozenset({"NoCopy", "Labels", "DriverConfig", "Subpath"})
TMPFS_OPTION_FIELDS = frozenset({"SizeBytes", "Mode", "Options"})
MAPS = frozenset(
    name.casefold()
    for name in """
    Volumes ExposedPorts PortBindings Sysctls StorageOpt Tmpfs Annotations
    EndpointsConfig Labels Config Options DriverOpts
    """.split()
)
"""Fields whose value is a map keyed by names (a path, a port, a network, an
option), casefolded: such a map is empty only when it has no keys at all.
"""
ENDPOINTS = "EndpointsConfig".casefold()
LOG_DRIVERS = frozenset({"json-file", "local"})
LOG_CEILING_BYTES = 16 * 1024 * 1024
"""The most a step's log may keep on the machine: max-size times max-file."""
LOG_SIZE = re.compile(r"([1-9][0-9]{0,9})([kmg]?)")
LOG_FILES = re.compile(r"[1-9][0-9]{0,2}")
LOG_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}

CONTAINER_VERBS: dict[tuple[str, str | None], str] = {
    ("POST", "start"): "start",
    ("POST", "wait"): "wait",
    ("POST", "kill"): "kill",
    ("POST", "stop"): "stop",
    ("GET", "json"): "inspect",
    ("GET", "logs"): "logs",
    ("DELETE", None): "delete",
}
QUERIES: dict[str, dict[str, re.Pattern[str] | frozenset[str]]] = {
    "ping": {},
    "version": {},
    "create": {},
    "start": {},
    "wait": {"condition": frozenset({"not-running", "next-exit", "removed"})},
    "kill": {"signal": re.compile(r"[A-Z0-9]{1,12}")},
    "stop": {
        "t": re.compile(r"[0-9]{1,5}"),
        "signal": re.compile(r"[A-Z0-9]{1,12}"),
    },
    "inspect": {},
    "logs": {
        "stdout": BOOLEAN,
        "stderr": BOOLEAN,
        "timestamps": BOOLEAN,
        "follow": frozenset({"0", "false"}),
        "tail": re.compile(r"[0-9]{1,9}|all"),
        "since": re.compile(r"[0-9]{1,12}"),
        "until": re.compile(r"[0-9]{1,12}"),
    },
    "delete": {"force": BOOLEAN, "v": BOOLEAN},
}


class RefusedError(Exception):
    """A request the filter will not pass, and why, for the harness to report."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Caller:
    """The container on the other end of a connection, as the daemon describes
    it. `grading` is its UNICON_GRADING_ID and `workspace` the volume it has
    at the workspace path; when the filter cannot say, `unknown` says why.
    """

    container: str | None = None
    grading: str | None = None
    workspace: str | None = None
    unknown: str | None = None

    def require(self) -> tuple[str, str, str]:
        if self.unknown is not None:
            raise RefusedError(f"the caller is not a grading run: {self.unknown}")
        if self.container is None or self.grading is None or self.workspace is None:
            raise RefusedError("the caller is not a grading run")
        return self.container, self.grading, self.workspace


@dataclass(frozen=True)
class Route:
    verb: str
    path: str
    container: str | None = None


def route(method: str, path: str, query: Iterable[tuple[str, str]]) -> Route:
    """Which of the allowed verbs a request is, with the path the daemon will be
    sent, or a RefusedError naming what it asked for.
    """
    matched = VERSIONED.fullmatch(path)
    bare = matched.group(1) if matched else path
    found: Route | None = None
    if bare == "/_ping" and method in ("GET", "HEAD"):
        found = Route("ping", path)
    elif bare == "/version" and method == "GET":
        found = Route("version", path)
    elif bare == "/containers/create" and method == "POST":
        found = Route("create", path)
    else:
        parts = bare.split("/")
        if len(parts) in (3, 4) and parts[1] == "containers":
            action = parts[3] if len(parts) == 4 else None
            verb = CONTAINER_VERBS.get((method, action))
            if verb is not None:
                if not CONTAINER.fullmatch(parts[2]):
                    raise RefusedError(
                        "containers are addressed by their full 64-character id"
                    )
                found = Route(verb, path, parts[2])
    if found is None:
        raise RefusedError(f"{method} {bare} is not something a grading run may do")
    _check_query(found.verb, query)
    return found


def _check_query(verb: str, query: Iterable[tuple[str, str]]) -> None:
    allowed = QUERIES[verb]
    for key, value in query:
        rule = allowed.get(key)
        if rule is None:
            raise RefusedError(f"the query parameter {key} is not accepted on {verb}")
        good = rule.fullmatch(value) if isinstance(rule, re.Pattern) else value in rule
        if not good:
            raise RefusedError(f"{key}={value} is not accepted on {verb}")


def parse_body(body: bytes) -> dict[str, Any]:
    """The create body as JSON, refusing a repeated key: the daemon keeps the
    last of two, and a check that read the first would be walked past.
    """

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise RefusedError("the body repeats a key")
        return dict(pairs)

    try:
        document = json.loads(body, object_pairs_hook=unique)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise RefusedError("the body is not JSON") from None
    if not isinstance(document, dict):
        raise RefusedError("the body is not a JSON object")
    return document


@dataclass(frozen=True)
class Ticket:
    """What an allowed create commits the filter to: the reaper's clock, and the
    body the daemon is sent. That body is the judged document written out
    again with the harness label added, never the caller's own bytes, so the
    daemon's JSON decoder reads exactly what the filter read.
    """

    grading: str
    time_ms: int
    body: bytes


def check_create(body: Mapping[str, Any], caller: Caller, config: Config) -> Ticket:
    own, grading, workspace = caller.require()
    _known(body, CONFIG_FIELDS, "")
    image = body.get("Image")
    if image not in config.images:
        raise RefusedError(f"image {image!r} is not on the machine's list")
    _check_user(body.get("User"))
    labels = body.get("Labels")
    time_ms = _check_labels(labels, grading, config)
    for name in ("Entrypoint", "Cmd", "Env"):
        _strings(body.get(name), name)
    if not isinstance(body.get("WorkingDir", ""), str):
        raise RefusedError("WorkingDir is not text")
    for name in ("AttachStdin", "Tty", "OpenStdin", "StdinOnce"):
        if not _zero(body.get(name)):
            raise RefusedError(
                f"{name} must be false: a step has no terminal and no input"
            )
    for name in ("AttachStdout", "AttachStderr", "NetworkDisabled"):
        if not isinstance(body.get(name, False), bool):
            raise RefusedError(f"{name} is not true or false")
    if not isinstance(body.get("StopSignal", ""), str):
        raise RefusedError("StopSignal is not text")
    if not isinstance(body.get("StopTimeout", 0), int | None):
        raise RefusedError("StopTimeout is not a number")
    _check_networking(body.get("NetworkingConfig"))
    carried = set(
        """
        Image User Labels Entrypoint Cmd Env WorkingDir AttachStdin Tty OpenStdin
        StdinOnce AttachStdout AttachStderr NetworkDisabled StopSignal StopTimeout
        HostConfig NetworkingConfig
        """.split()
    )
    _rest_empty(body, carried, "")
    host = body.get("HostConfig")
    if not isinstance(host, dict):
        raise RefusedError("HostConfig is missing")
    _check_host(host, workspace, config)
    forwarded = dict(body)
    forwarded["Labels"] = {**dict(labels or {}), HARNESS_LABEL: own}
    return Ticket(grading, time_ms, json.dumps(forwarded).encode())


def _check_networking(networking: Any) -> None:
    """No network at all: NetworkingConfig is absent, or names no endpoint, in
    whatever case its one field is spelt. The CLI sends `{"EndpointsConfig":
    {}}`; a step needs nothing more.
    """
    if networking is None:
        return
    if not isinstance(networking, dict) or not all(
        isinstance(key, str)
        and key.casefold() == ENDPOINTS
        and (value is None or value == {})
        for key, value in networking.items()
    ):
        raise RefusedError("NetworkingConfig must be empty: a step has no network")


def _check_user(user: Any) -> None:
    matched = USER.fullmatch(user) if isinstance(user, str) else None
    if matched is None:
        raise RefusedError("the step must run as a numeric uid:gid")
    if int(matched.group(1)) == 0 or int(matched.group(2)) == 0:
        raise RefusedError("the step must not run as root or in the root group")


def _check_labels(labels: Any, grading: str, config: Config) -> int:
    """The step's labels: the run's grading and the step's clock, and the step's
    id if given, and nothing else. Other labels are what other software on the
    machine goes by (a compose project, the CI's own), and a step has no
    business being mistaken for one of theirs.
    """
    if not isinstance(labels, dict) or not all(
        isinstance(v, str) for v in labels.values()
    ):
        raise RefusedError(f"the {GRADING_LABEL} and {TIME_LABEL} labels are missing")
    for key in labels:
        if key not in LABELS:
            raise RefusedError(
                f"the label {key!r} is not one a step may carry: only "
                f"{', '.join(sorted(LABELS))}"
            )
    if not same_grading(labels.get(GRADING_LABEL), grading):
        raise RefusedError(f"the {GRADING_LABEL} label is not the caller's own grading")
    step = labels.get(STEP_LABEL)
    if step is not None and not STEP.fullmatch(step):
        raise RefusedError(f"the {STEP_LABEL} label is not a step id")
    raw = labels.get(TIME_LABEL, "")
    if not CLOCK.fullmatch(raw) or not 1 <= int(raw) <= config.max_time_ms:
        raise RefusedError(
            f"the {TIME_LABEL} label must be the step's wall clock, 1 to "
            f"{config.max_time_ms} ms"
        )
    return int(raw)


def _check_host(host: Mapping[str, Any], workspace: str, config: Config) -> None:
    _known(host, HOST_FIELDS, "HostConfig.")
    if host.get("NetworkMode") != "none":
        raise RefusedError(f"NetworkMode must be none, not {host.get('NetworkMode')!r}")
    if host.get("ReadonlyRootfs") is not True:
        raise RefusedError("ReadonlyRootfs must be true")
    if host.get("CapDrop") != ["ALL"]:
        raise RefusedError("CapDrop must be exactly ALL")
    options = host.get("SecurityOpt")
    if (
        not isinstance(options, list)
        or len(options) != len(SECURITY_OPTIONS)
        or set(options) != SECURITY_OPTIONS
    ):
        raise RefusedError(f"SecurityOpt must be exactly {sorted(SECURITY_OPTIONS)}")
    memory = _positive(host.get("Memory"), "a memory limit (Memory)")
    if memory > config.max_memory_mb * 1024 * 1024:
        raise RefusedError(
            f"Memory is above the machine's ceiling of {config.max_memory_mb} MB"
        )
    if host.get("MemorySwap") != memory:
        raise RefusedError("MemorySwap must equal Memory: no swap")
    cpus = _positive(host.get("NanoCpus"), "a CPU limit (NanoCpus)")
    if cpus > config.max_cpus * 1_000_000_000:
        raise RefusedError(
            f"NanoCpus is above the machine's ceiling of {config.max_cpus} CPUs"
        )
    pids = _positive(host.get("PidsLimit"), "a pids limit (PidsLimit)")
    if pids > config.max_pids:
        raise RefusedError(
            f"PidsLimit is above the machine's ceiling of {config.max_pids}"
        )
    _check_ulimits(host.get("Ulimits"))
    _check_mounts(host.get("Mounts"), workspace, memory)
    _check_log(host.get("LogConfig"))
    restart = host.get("RestartPolicy")
    if restart is not None and (
        not isinstance(restart, dict)
        or restart.get("Name", "") not in ("", "no")
        or not _zero(restart.get("MaximumRetryCount"))
        or set(restart) - {"Name", "MaximumRetryCount"}
    ):
        raise RefusedError("no restart policy")
    if host.get("IpcMode", "") not in ("", "private", "none"):
        raise RefusedError("IpcMode must be private or none")
    if host.get("CgroupnsMode", "") not in ("", "private"):
        raise RefusedError("CgroupnsMode must be private")
    if host.get("MemorySwappiness") not in (None, -1, 0):
        raise RefusedError("MemorySwappiness stays at the default")
    for name in ("MaskedPaths", "ReadonlyPaths", "Capabilities"):
        if host.get(name) is not None:
            raise RefusedError(f"{name} stays at the daemon default")
    carried = set(
        """
        NetworkMode ReadonlyRootfs CapDrop SecurityOpt Memory MemorySwap NanoCpus
        PidsLimit Ulimits Mounts LogConfig RestartPolicy IpcMode CgroupnsMode
        MemorySwappiness MaskedPaths ReadonlyPaths Capabilities
        """.split()
    )
    _rest_empty(host, carried, "HostConfig.")


def _check_ulimits(ulimits: Any) -> None:
    if ulimits is None:
        return
    if not isinstance(ulimits, list):
        raise RefusedError("Ulimits is not a list")
    seen: set[str] = set()
    for entry in ulimits:
        if not isinstance(entry, dict) or set(entry) != {"Name", "Soft", "Hard"}:
            raise RefusedError("a ulimit is not Name, Soft and Hard")
        name = entry["Name"]
        if name not in ("cpu", "fsize") or name in seen:
            raise RefusedError("only the cpu and fsize ulimits may be set, once each")
        seen.add(name)
        soft = _positive(entry["Soft"], f"the {name} ulimit")
        if entry["Hard"] != soft:
            raise RefusedError(f"the {name} ulimit must have Soft equal to Hard")


def _check_mounts(mounts: Any, workspace: str, memory: int) -> None:
    """Exactly the run's workspace at /work, as a directory of its own under
    unicon-steps/ in the caller's workspace volume, and at most a tmpfs at
    /tmp. A bind mount of any path is refused: a host path is not something
    the filter can tie to the run.
    """
    if not isinstance(mounts, list) or not all(isinstance(m, dict) for m in mounts):
        raise RefusedError("Mounts must list the run's workspace")
    targets = [m.get("Target") for m in mounts]
    if len(set(targets)) != len(targets):
        raise RefusedError("two mounts share a target")
    work = [m for m in mounts if m.get("Target") == WORK_TARGET]
    if len(work) != 1:
        raise RefusedError("the run's workspace must be mounted at /work")
    for mount in mounts:
        _known(mount, MOUNT_FIELDS, "a mount's ")
        target = mount.get("Target")
        if target == WORK_TARGET:
            _check_work(mount, workspace)
        elif target == TMP_TARGET:
            _check_tmp(mount, memory)
        else:
            raise RefusedError(
                f"a mount at {target!r}: only /work and /tmp are allowed"
            )


def _check_work(mount: Mapping[str, Any], workspace: str) -> None:
    if mount.get("Type") != "volume":
        raise RefusedError("/work must be a volume mount of the run's workspace")
    if mount.get("Source") != workspace:
        raise RefusedError(f"volume {mount.get('Source')!r} is not the run's workspace")
    options = mount.get("VolumeOptions")
    if not isinstance(options, dict):
        raise RefusedError(
            "/work must be a unicon-steps/ subpath of the run's workspace"
        )
    _known(options, VOLUME_OPTION_FIELDS, "VolumeOptions.")
    subpath = options.get("Subpath")
    if not isinstance(subpath, str) or not SUBPATH.fullmatch(subpath):
        raise RefusedError(
            "/work must be a unicon-steps/ subpath of the run's workspace"
        )
    if options.get("NoCopy") is not True:
        raise RefusedError(
            "VolumeOptions.NoCopy must be true: the daemon copies nothing into the "
            "run's workspace"
        )
    _rest_empty(options, {"Subpath", "NoCopy"}, "VolumeOptions.")
    if not _zero(mount.get("ReadOnly")):
        raise RefusedError("/work is the step's writable directory")
    _rest_empty(
        mount,
        {"Type", "Source", "Target", "VolumeOptions", "ReadOnly"},
        "the /work mount's ",
    )


def _check_tmp(mount: Mapping[str, Any], memory: int) -> None:
    if mount.get("Type") != "tmpfs":
        raise RefusedError("/tmp must be a tmpfs")
    options = mount.get("TmpfsOptions")
    if not isinstance(options, dict):
        raise RefusedError("the /tmp tmpfs needs a size")
    _known(options, TMPFS_OPTION_FIELDS, "TmpfsOptions.")
    size = _positive(options.get("SizeBytes"), "a size for the /tmp tmpfs")
    if size > memory:
        raise RefusedError("the /tmp tmpfs is larger than the step's memory")
    mode = options.get("Mode", 0o1777)
    if type(mode) is not int or not 0 <= mode <= 0o7777:
        raise RefusedError("the /tmp tmpfs mode is not a file mode")
    _rest_empty(options, {"SizeBytes", "Mode"}, "TmpfsOptions.")
    _rest_empty(mount, {"Type", "Target", "TmpfsOptions"}, "the /tmp mount's ")


def _check_log(log: Any) -> None:
    """A log the machine keeps within bounds: the json-file or local driver,
    with max-size and max-file both set and their product at most
    LOG_CEILING_BYTES. The machine's default driver may keep everything a
    step prints, so it is not taken.
    """
    if not isinstance(log, dict) or set(log) - {"Type", "Config"}:
        raise RefusedError("LogConfig must name a log driver and its bounds")
    if log.get("Type") not in LOG_DRIVERS:
        raise RefusedError("the log driver must be json-file or local")
    config = log.get("Config")
    if (
        not isinstance(config, dict)
        or set(config) != {"max-size", "max-file"}
        or not all(isinstance(v, str) for v in config.values())
    ):
        raise RefusedError("LogConfig.Config must set max-size and max-file, only")
    size = LOG_SIZE.fullmatch(config["max-size"])
    files = LOG_FILES.fullmatch(config["max-file"])
    if size is None or files is None:
        raise RefusedError(
            "max-size must be a whole number of bytes, k, m or g, and max-file "
            "a whole number of files"
        )
    kept = int(size.group(1)) * LOG_UNITS[size.group(2)] * int(files.group(0))
    if kept > LOG_CEILING_BYTES:
        raise RefusedError(
            "max-size times max-file is above the machine's ceiling of "
            f"{LOG_CEILING_BYTES // (1024 * 1024)} MB of log per step"
        )


def _known(document: Mapping[str, Any], fields: frozenset[str], where: str) -> None:
    for key in document:
        if key not in fields:
            raise RefusedError(f"{where}{key} is not a field this filter knows")


def _rest_empty(document: Mapping[str, Any], carried: set[str], where: str) -> None:
    for key, value in document.items():
        if key not in carried and not _zero(value, key):
            raise RefusedError(f"{where}{key} must be empty")


def _zero(value: Any, key: str | None = None) -> bool:
    """Whether a value is what an unset field decodes to: null, false, 0, empty
    text, an empty list or map, a list of zeros (the CLI's `ConsoleSize`), or a
    structure of such values. The docker CLI sends many fields this way; none
    of them changes the container. A map, a field whose keys are names (a
    path, a port, a network), is empty only when it has no keys: `{"/data":
    {}}` asks for an anonymous volume. A list holding empty text or an empty
    object is not empty: `Binds: [""]` or `Devices: [{}]` is an entry the
    daemon would try to read. Field names are compared without regard to
    case, as the daemon's decoder reads them: `{"endpointsconfig": {"host":
    {}}}` names a network.
    """
    if value is None or value is False or value == "":
        return True
    if type(value) in (int, float):
        return bool(value == 0)
    if isinstance(value, list):
        return all(type(v) in (int, float) and v == 0 for v in value)
    if isinstance(value, dict):
        if isinstance(key, str) and key.casefold() in MAPS:
            return not value
        return all(_zero(v, k) for k, v in value.items())
    return False


def _positive(value: Any, what: str) -> int:
    if type(value) is not int or value <= 0:
        raise RefusedError(f"{what} is required")
    return value


def _strings(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RefusedError(f"{name} is not a list of text")


def same_grading(one: Any, other: Any) -> bool:
    try:
        return uuid.UUID(str(one)) == uuid.UUID(str(other))
    except ValueError:
        return False


def normal_grading(value: Any) -> str | None:
    """A grading id in one spelling, or None when it is not one."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None
