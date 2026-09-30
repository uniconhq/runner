"""What the policy passes and refuses, request by request. The create checks are
the prototype's (tools/socket-policy-test in the proposal repo), C for more than
the sandbox, D for less, R for bodies the docker CLI cannot express, written as
the body the daemon would receive.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from filter_tests.support import (
    CALLER,
    CONFIG,
    GRADING,
    HARNESS,
    MIB,
    OTHER_GRADING,
    WORKSPACE,
    body,
)
from unicon_filter.policy import (
    Caller,
    RefusedError,
    check_create,
    parse_body,
    route,
)

Change = Callable[[dict[str, Any]], None]


def refused(document: dict[str, Any], caller: Caller = CALLER) -> str:
    with pytest.raises(RefusedError) as refusal:
        check_create(document, caller, CONFIG)
    return refusal.value.reason


def changed(change: Change) -> dict[str, Any]:
    document = body()
    change(document)
    return document


def host(**fields: Any) -> Change:
    return lambda d: d["HostConfig"].update(fields)


def without(*path: str) -> Change:
    def change(document: dict[str, Any]) -> None:
        node = document
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return change


def test_the_harness_body_passes_with_its_clock() -> None:
    ticket = check_create(body(), CALLER, CONFIG)
    assert ticket.grading == GRADING
    assert ticket.time_ms == 60000


def test_the_daemon_is_sent_the_judged_document_with_the_harness_label() -> None:
    """Not the caller's bytes: the document as the filter read it, written out
    again, with the label that names the harness the step belongs to.
    """
    sent = json.loads(check_create(body(), CALLER, CONFIG).body)
    expected = body()
    expected["Labels"]["unicon.harness"] = HARNESS
    assert sent == expected


def test_the_docker_cli_way_of_sending_unset_fields_passes() -> None:
    """The CLI sends every field, most of them empty; none of them changes the
    container, so none is refused.
    """
    document = body()
    document.update(
        Hostname="",
        Domainname="",
        AttachStdin=False,
        AttachStdout=True,
        AttachStderr=True,
        Tty=False,
        OpenStdin=False,
        StdinOnce=False,
        Cmd=None,
        Volumes={},
        NetworkingConfig={"EndpointsConfig": {}},
    )
    document["HostConfig"].update(
        Binds=None,
        ContainerIDFile="",
        PortBindings={},
        AutoRemove=False,
        VolumeDriver="",
        VolumesFrom=None,
        ConsoleSize=[0, 0],
        CapAdd=None,
        CgroupnsMode="",
        Dns=None,
        DnsOptions=[],
        DnsSearch=[],
        ExtraHosts=None,
        GroupAdd=None,
        IpcMode="",
        Cgroup="",
        Links=None,
        OomScoreAdj=0,
        PidMode="",
        Privileged=False,
        PublishAllPorts=False,
        UTSMode="",
        UsernsMode="",
        ShmSize=0,
        Isolation="",
        CpuShares=0,
        CgroupParent="",
        BlkioWeight=0,
        BlkioWeightDevice=[],
        CpuPeriod=0,
        CpuQuota=0,
        CpusetCpus="",
        Devices=[],
        DeviceCgroupRules=None,
        DeviceRequests=None,
        MemoryReservation=0,
        MemorySwappiness=-1,
        OomKillDisable=False,
        CpuCount=0,
        IOMaximumIOps=0,
        RestartPolicy={"Name": "no", "MaximumRetryCount": 0},
        MaskedPaths=None,
        ReadonlyPaths=None,
    )
    assert check_create(document, CALLER, CONFIG).time_ms == 60000


MORE_THAN_THE_SANDBOX: list[tuple[str, Change, str]] = [
    ("C1 privileged", host(Privileged=True), "Privileged must be empty"),
    ("C2 bind the host root", host(Binds=["/:/host"]), "Binds must be empty"),
    (
        "C3 bind the real docker socket",
        lambda d: d["HostConfig"]["Mounts"].append(
            {"Type": "bind", "Source": "/var/run/docker.sock", "Target": "/s"}
        ),
        "only /work and /tmp",
    ),
    (
        "C4 mount the filter socket's own volume",
        lambda d: d["HostConfig"]["Mounts"].append(
            {"Type": "volume", "Source": "unicon-filter", "Target": "/p"}
        ),
        "only /work and /tmp",
    ),
    ("C5 host network", host(NetworkMode="host"), "NetworkMode must be none"),
    ("C6 bridge network", host(NetworkMode="bridge"), "NetworkMode must be none"),
    ("C7 cap-add SYS_ADMIN", host(CapAdd=["SYS_ADMIN"]), "CapAdd must be empty"),
    (
        "C8 seccomp unconfined",
        host(SecurityOpt=["no-new-privileges", "seccomp=unconfined"]),
        "SecurityOpt",
    ),
    ("C9 host pid namespace", host(PidMode="host"), "PidMode must be empty"),
    ("C10 host ipc namespace", host(IpcMode="host"), "IpcMode"),
    ("C11 a device", host(Devices=[{"PathOnHost": "/dev/null"}]), "Devices"),
    ("C14 a restart policy", host(RestartPolicy={"Name": "always"}), "restart"),
    (
        "C15 a published port",
        host(PortBindings={"80/tcp": [{"HostPort": "80"}]}),
        "PortBindings",
    ),
    (
        "C16 a sysctl",
        host(Sysctls={"net.ipv4.ip_unprivileged_port_start": "0"}),
        "Sysctls",
    ),
    ("C17 userns host", host(UsernsMode="host"), "UsernsMode"),
    ("C18 a cgroup parent", host(CgroupParent="/unicon-escape"), "CgroupParent"),
    (
        "apparmor unconfined",
        host(
            SecurityOpt=["no-new-privileges", "seccomp=builtin", "apparmor=unconfined"]
        ),
        "SecurityOpt",
    ),
    ("another runtime", host(Runtime="nvidia"), "Runtime must be empty"),
    ("an oom score that shields it", host(OomScoreAdj=-1000), "OomScoreAdj"),
    ("oom kill disabled", host(OomKillDisable=True), "OomKillDisable"),
    (
        "a raised nofile ulimit",
        host(Ulimits=[{"Name": "nofile", "Soft": 1 << 20, "Hard": 1 << 20}]),
        "only the cpu and fsize",
    ),
    ("memory above the machine", host(Memory=1 << 40, MemorySwap=1 << 40), "ceiling"),
    ("pids above the machine", host(PidsLimit=1 << 20), "ceiling"),
    ("cpus above the machine", host(NanoCpus=64_000_000_000), "ceiling"),
    (
        "a syslog log driver",
        host(LogConfig={"Type": "syslog", "Config": {}}),
        "log driver",
    ),
    ("the machine's own log driver", without("HostConfig", "LogConfig"), "LogConfig"),
    (
        "the machine's own log driver, named empty",
        host(LogConfig={"Type": "", "Config": {"max-size": "8m", "max-file": "1"}}),
        "log driver",
    ),
    (
        "a log without a size",
        host(LogConfig={"Type": "json-file", "Config": {"max-file": "1"}}),
        "max-size and max-file",
    ),
    (
        "a log without a file count",
        host(LogConfig={"Type": "local", "Config": {"max-size": "8m"}}),
        "max-size and max-file",
    ),
    (
        "a log above the ceiling",
        host(
            LogConfig={
                "Type": "json-file",
                "Config": {"max-size": "1g", "max-file": "1"},
            }
        ),
        "ceiling",
    ),
    (
        "a log above the ceiling in many files",
        host(
            LogConfig={"Type": "local", "Config": {"max-size": "8m", "max-file": "3"}}
        ),
        "ceiling",
    ),
    (
        "a log size in a unit the filter does not read",
        host(
            LogConfig={
                "Type": "json-file",
                "Config": {"max-size": "8.5MiB", "max-file": "1"},
            }
        ),
        "whole number",
    ),
    (
        "a log that also compresses",
        host(
            LogConfig={
                "Type": "local",
                "Config": {"max-size": "8m", "max-file": "1", "compress": "false"},
            }
        ),
        "max-size and max-file",
    ),
    ("a terminal", lambda d: d.update(Tty=True), "Tty must be false"),
    ("stdin held open", lambda d: d.update(OpenStdin=True), "OpenStdin"),
    (
        "an exposed port",
        lambda d: d.update(ExposedPorts={"80/tcp": {}}),
        "ExposedPorts must be empty",
    ),
    (
        "an attached network",
        lambda d: d.update(NetworkingConfig={"EndpointsConfig": {"bridge": {"x": 1}}}),
        "NetworkingConfig must be empty",
    ),
]

LESS_THAN_THE_SANDBOX: list[tuple[str, Change, str]] = [
    ("D1 without cap-drop ALL", without("HostConfig", "CapDrop"), "CapDrop"),
    ("D1 cap-drop of one", host(CapDrop=["NET_RAW"]), "CapDrop"),
    ("D2 without read-only root", host(ReadonlyRootfs=False), "ReadonlyRootfs"),
    (
        "D3 without no-new-privileges and seccomp",
        without("HostConfig", "SecurityOpt"),
        "SecurityOpt",
    ),
    (
        "D3 seccomp left to the daemon",
        host(SecurityOpt=["no-new-privileges"]),
        "SecurityOpt",
    ),
    ("D4 without a memory limit", without("HostConfig", "Memory"), "memory limit"),
    ("D5 without a pids limit", without("HostConfig", "PidsLimit"), "pids limit"),
    ("without a cpu limit", without("HostConfig", "NanoCpus"), "CPU limit"),
    ("D6 as root, no user", without("User"), "numeric uid:gid"),
    ("D7 as root, user 0", lambda d: d.update(User="0:0"), "not run as root"),
    ("as root by name", lambda d: d.update(User="root"), "numeric uid:gid"),
    ("in the root group", lambda d: d.update(User="10001:0"), "root group"),
    ("D8 without the run label", without("Labels", "unicon.grading"), "unicon.grading"),
    ("without the clock label", without("Labels", "unicon.time_ms"), "unicon.time_ms"),
    (
        "a clock beyond the machine's",
        lambda d: d["Labels"].update({"unicon.time_ms": str(10**12)}),
        "unicon.time_ms",
    ),
    ("D9 with swap allowed", host(MemorySwap=512 * MIB), "no swap"),
    ("D10 on the default network", without("HostConfig", "NetworkMode"), "NetworkMode"),
    ("without the workspace", host(Mounts=[]), "mounted at /work"),
]

RAW_BODIES: list[tuple[str, Change, str]] = [
    (
        "the daemon copying image content into the workspace",
        lambda d: d["HostConfig"]["Mounts"][0]["VolumeOptions"].pop("NoCopy"),
        "NoCopy must be true",
    ),
    ("R masked paths emptied", host(MaskedPaths=[]), "MaskedPaths"),
    ("R read-only paths emptied", host(ReadonlyPaths=[]), "ReadonlyPaths"),
    (
        "R the legacy Capabilities field",
        host(Capabilities=["CAP_SYS_ADMIN"]),
        "Capabilities",
    ),
    (
        "R a mount of /etc",
        lambda d: d["HostConfig"]["Mounts"].append(
            {"Type": "bind", "Source": "/etc", "Target": "/x"}
        ),
        "only /work and /tmp",
    ),
    (
        "R an anonymous volume",
        lambda d: d.update(Volumes={"/data": {}}),
        "Volumes must be empty",
    ),
    (
        "a field in another case",
        lambda d: d["HostConfig"].update(privileged=True),
        "not a field this filter knows",
    ),
    (
        "a field in another case at the top",
        lambda d: d.update(hostconfig={"Privileged": True}),
        "not a field",
    ),
    ("a field docker does not know", lambda d: d.update(Surprise=1), "not a field"),
    (
        "the workspace as a bind mount",
        host(
            Mounts=[
                {
                    "Type": "bind",
                    "Source": "/var/lib/docker/volumes/x/_data",
                    "Target": "/work",
                }
            ]
        ),
        "volume mount",
    ),
    (
        "another run's workspace",
        lambda d: d["HostConfig"]["Mounts"][0].update(Source="wp_other_default"),
        "is not the run's workspace",
    ),
    (
        "the whole workspace, checkouts and all",
        lambda d: d["HostConfig"]["Mounts"][0].update(VolumeOptions={}),
        "subpath",
    ),
    (
        "the task checkout",
        lambda d: d["HostConfig"]["Mounts"][0].update(
            VolumeOptions={"Subpath": "task"}
        ),
        "subpath",
    ),
    (
        "a subpath climbing out",
        lambda d: d["HostConfig"]["Mounts"][0].update(
            VolumeOptions={"Subpath": "unicon-steps/../task"}
        ),
        "subpath",
    ),
    (
        "a nested subpath",
        lambda d: d["HostConfig"]["Mounts"][0].update(
            VolumeOptions={"Subpath": "unicon-steps/a/b"}
        ),
        "subpath",
    ),
    (
        "a volume driver option",
        lambda d: d["HostConfig"]["Mounts"][0]["VolumeOptions"].update(
            DriverConfig={"Name": "local", "Options": {"device": "/"}}
        ),
        "must be empty",
    ),
    (
        "the workspace read-only and bound again",
        lambda d: d["HostConfig"]["Mounts"][0].update(
            BindOptions={"Propagation": "shared"}
        ),
        "must be empty",
    ),
    (
        "a tmpfs larger than memory",
        lambda d: d["HostConfig"]["Mounts"][1]["TmpfsOptions"].update(
            SizeBytes=1 << 40
        ),
        "larger than the step's memory",
    ),
    (
        "a tmpfs with its own options",
        lambda d: d["HostConfig"]["Mounts"][1]["TmpfsOptions"].update(
            Options=[["exec"]]
        ),
        "must be empty",
    ),
    ("the Tmpfs map", host(Tmpfs={"/run": "exec"}), "Tmpfs must be empty"),
    ("memory given as a boolean", host(Memory=True, MemorySwap=True), "memory limit"),
    ("a bind given as empty text", host(Binds=[""]), "Binds must be empty"),
    ("a device given as an empty object", host(Devices=[{}]), "Devices"),
    ("a capability given as empty text", host(CapAdd=[""]), "CapAdd must be empty"),
    ("a device rule given as empty text", host(DeviceCgroupRules=[""]), "Device"),
    (
        "a label another program on the machine goes by",
        lambda d: d["Labels"].update({"com.docker.compose.project": "unicon"}),
        "not one a step may carry",
    ),
    (
        "the harness label, which only the filter writes",
        lambda d: d["Labels"].update({"unicon.harness": "b" * 64}),
        "not one a step may carry",
    ),
    (
        "a clock in digits that are not ASCII",
        lambda d: d["Labels"].update({"unicon.time_ms": "٣٠٠٠"}),
        "unicon.time_ms",
    ),
    (
        "a clock in superscript digits",
        lambda d: d["Labels"].update({"unicon.time_ms": "²"}),
        "unicon.time_ms",
    ),
    (
        "a step label that is not a step id",
        lambda d: d["Labels"].update({"unicon.step": "a b"}),
        "not a step id",
    ),
    ("a user with a line end", lambda d: d.update(User="10001:10001\n"), "uid:gid"),
    (
        "a subpath with a line end",
        lambda d: d["HostConfig"]["Mounts"][0].update(
            VolumeOptions={"Subpath": "unicon-steps/a\n", "NoCopy": True}
        ),
        "subpath",
    ),
    (
        "a healthcheck that runs a command",
        lambda d: d.update(Healthcheck={"Test": ["CMD", "true"]}),
        "Healthcheck must be empty",
    ),
    (
        "an init process",
        host(Init=True),
        "Init must be empty",
    ),
    ("an annotation", host(Annotations={"io.kubernetes.cri.x": "1"}), "Annotations"),
    ("shared ipc", host(IpcMode="shareable"), "IpcMode"),
    ("host cgroup namespace", host(CgroupnsMode="host"), "CgroupnsMode"),
    ("host uts namespace", host(UTSMode="host"), "UTSMode must be empty"),
    ("another container's volumes", host(VolumesFrom=["x"]), "VolumesFrom"),
    ("a link", host(Links=["x:y"]), "Links must be empty"),
    ("an extra host", host(ExtraHosts=["a:1.2.3.4"]), "ExtraHosts must be empty"),
    ("a gpu", host(DeviceRequests=[{"Count": -1}]), "DeviceRequests"),
    ("a storage option", host(StorageOpt={"size": "1G"}), "StorageOpt"),
    ("a volume driver", host(VolumeDriver="local"), "VolumeDriver must be empty"),
    ("an added group", host(GroupAdd=["docker"]), "GroupAdd must be empty"),
    ("a bigger /dev/shm", host(ShmSize=1 << 30), "ShmSize must be empty"),
    (
        "a ulimit raised above its soft limit",
        host(Ulimits=[{"Name": "cpu", "Soft": 1, "Hard": 100}]),
        "Soft equal to Hard",
    ),
]


@pytest.mark.parametrize(
    ("name", "change", "reason"),
    MORE_THAN_THE_SANDBOX + LESS_THAN_THE_SANDBOX + RAW_BODIES,
    ids=[c[0] for c in MORE_THAN_THE_SANDBOX + LESS_THAN_THE_SANDBOX + RAW_BODIES],
)
def test_a_create_is_refused(name: str, change: Change, reason: str) -> None:
    assert reason in refused(changed(change))


ODD_CASE: list[tuple[str, Change, str]] = [
    (
        "an endpoint under a lower-case field",
        lambda d: d.update(NetworkingConfig={"endpointsconfig": {"host": {}}}),
        "NetworkingConfig must be empty",
    ),
    (
        "an endpoint under an upper-case field",
        lambda d: d.update(NetworkingConfig={"ENDPOINTSCONFIG": {"bridge": {}}}),
        "NetworkingConfig must be empty",
    ),
    (
        "an endpoint beside the empty field in another case",
        lambda d: d.update(
            NetworkingConfig={"EndpointsConfig": {}, "endpointsConfig": {"host": {}}}
        ),
        "NetworkingConfig must be empty",
    ),
    (
        "an endpoint with nothing set",
        lambda d: d.update(NetworkingConfig={"EndpointsConfig": {"host": None}}),
        "NetworkingConfig must be empty",
    ),
    (
        "NetworkingConfig in another case",
        lambda d: d.update(networkingconfig={"EndpointsConfig": {"host": {}}}),
        "not a field this filter knows",
    ),
    (
        "a field of NetworkingConfig the daemon does not know",
        lambda d: d.update(NetworkingConfig={"Surprise": {}}),
        "NetworkingConfig must be empty",
    ),
    (
        "a mount field in another case",
        lambda d: d["HostConfig"]["Mounts"][0].update(volumeOptions={"Subpath": "x"}),
        "not a field this filter knows",
    ),
    (
        "a volume option in another case",
        lambda d: d["HostConfig"]["Mounts"][0]["VolumeOptions"].update(nocopy=False),
        "not a field this filter knows",
    ),
    (
        "a tmpfs option in another case",
        lambda d: d["HostConfig"]["Mounts"][1]["TmpfsOptions"].update(
            options=[["exec"]]
        ),
        "not a field this filter knows",
    ),
    (
        "a volume driver option under a lower-case field",
        lambda d: d["HostConfig"]["Mounts"][0]["VolumeOptions"].update(
            DriverConfig={"options": {"device": ""}}
        ),
        "must be empty",
    ),
    (
        "a volume driver option with empty text",
        lambda d: d["HostConfig"]["Mounts"][0]["VolumeOptions"].update(
            DriverConfig={"Options": {"o": ""}}
        ),
        "must be empty",
    ),
    (
        "a volume label under a lower-case field",
        lambda d: d["HostConfig"]["Mounts"][0]["VolumeOptions"].update(
            labels={"x": ""}
        ),
        "not a field this filter knows",
    ),
    (
        "a bind option in another case",
        lambda d: d["HostConfig"]["Mounts"][0].update(
            BindOptions={"createmountpoint": True}
        ),
        "must be empty",
    ),
    (
        "an image option in another case",
        lambda d: d["HostConfig"]["Mounts"][0].update(ImageOptions={"SUBPATH": "x"}),
        "must be empty",
    ),
    (
        "a healthcheck in another case",
        lambda d: d.update(Healthcheck={"test": ["CMD", "true"]}),
        "Healthcheck must be empty",
    ),
    (
        "a log field in another case",
        host(LogConfig={"type": "syslog", "Config": {}}),
        "LogConfig must name",
    ),
    (
        "a restart field in another case",
        host(RestartPolicy={"name": "always"}),
        "no restart policy",
    ),
    (
        "a ulimit field in another case",
        host(Ulimits=[{"name": "nofile", "soft": 1 << 20, "hard": 1 << 20}]),
        "a ulimit is not Name, Soft and Hard",
    ),
    ("a host field in upper case", host(PRIVILEGED=True), "not a field"),
    ("a map in another case", host(portbindings={"80/tcp": []}), "not a field"),
]


@pytest.mark.parametrize(
    ("name", "change", "reason"), ODD_CASE, ids=[c[0] for c in ODD_CASE]
)
def test_a_field_name_in_another_case_is_read_as_the_daemon_reads_it(
    name: str, change: Change, reason: str
) -> None:
    """The daemon's decoder ignores the case of field names, so every level of
    the body is refused in any case the filter would otherwise pass as empty.
    """
    assert reason in refused(changed(change))


@pytest.mark.parametrize(
    "networking",
    [None, {}, {"EndpointsConfig": {}}, {"endpointsconfig": None}],
)
def test_a_create_naming_no_network_passes(networking: Any) -> None:
    document = changed(lambda d: d.update(NetworkingConfig=networking))
    assert check_create(document, CALLER, CONFIG).time_ms == 60000


@pytest.mark.parametrize(
    "log",
    [
        {"Type": "json-file", "Config": {"max-size": "8m", "max-file": "1"}},
        {"Type": "json-file", "Config": {"max-size": "16m", "max-file": "1"}},
        {"Type": "local", "Config": {"max-size": "8m", "max-file": "2"}},
        {"Type": "local", "Config": {"max-size": "1024k", "max-file": "16"}},
        {"Type": "json-file", "Config": {"max-size": "65536", "max-file": "1"}},
    ],
)
def test_a_bounded_log_passes(log: dict[str, Any]) -> None:
    document = changed(host(LogConfig=log))
    assert check_create(document, CALLER, CONFIG).time_ms == 60000


@pytest.mark.parametrize(
    "image",
    [
        "alpine:latest",
        "busybox",
        f"ghcr.io/uniconhq/primitive-compile@sha256:{'2' * 64}",
        f"ghcr.io/evil/primitive-compile@sha256:{'1' * 64}",
        f"sha256:{'1' * 64}",
    ],
)
def test_an_image_not_on_the_list_is_refused(image: str) -> None:
    """C12 and C13, and the same digest under another name."""
    assert "is not on the machine's list" in refused(
        changed(lambda d: d.update(Image=image))
    )


def test_a_create_for_another_grading_is_refused() -> None:
    document = changed(lambda d: d["Labels"].update({"unicon.grading": OTHER_GRADING}))
    assert "not the caller's own grading" in refused(document)


def test_the_grading_label_matches_whatever_its_case() -> None:
    document = changed(
        lambda d: d["Labels"].update({"unicon.grading": GRADING.upper()})
    )
    assert check_create(document, CALLER, CONFIG).grading == GRADING


def test_a_caller_that_is_not_a_grading_run_cannot_create() -> None:
    assert "not a grading run: no volume" in refused(
        body(), Caller(HARNESS, GRADING, unknown="no volume")
    )


def test_the_workspace_is_the_callers_own() -> None:
    other = Caller(HARNESS, GRADING, "wp_other_default")
    assert "is not the run's workspace" in refused(body(), other)
    assert check_create(body(), Caller(HARNESS, GRADING, WORKSPACE), CONFIG)


def test_a_body_repeating_a_key_is_refused() -> None:
    raw = json.dumps(body())[:-1] + ', "Image": "alpine:3"}'
    with pytest.raises(RefusedError, match="repeats a key"):
        parse_body(raw.encode())


@pytest.mark.parametrize("raw", [b"", b"[]", b"{", b"\xff\xfe", b'{"a": 1} {"b": 2}'])
def test_a_body_that_is_not_one_json_object_is_refused(raw: bytes) -> None:
    with pytest.raises(RefusedError):
        parse_body(raw)


ALLOWED = [
    ("GET", "/_ping", ()),
    ("HEAD", "/_ping", ()),
    ("GET", "/v1.45/version", ()),
    ("POST", "/v1.45/containers/create", ()),
    ("POST", "/containers/create", ()),
    ("POST", f"/v1.45/containers/{'b' * 64}/start", ()),
    ("POST", f"/v1.45/containers/{'b' * 64}/wait", (("condition", "not-running"),)),
    ("POST", f"/v1.45/containers/{'b' * 64}/kill", (("signal", "KILL"),)),
    ("POST", f"/v1.45/containers/{'b' * 64}/stop", (("t", "5"),)),
    ("GET", f"/v1.45/containers/{'b' * 64}/json", ()),
    ("GET", f"/v1.45/containers/{'b' * 64}/logs", (("stdout", "1"), ("stderr", "1"))),
    ("DELETE", f"/v1.45/containers/{'b' * 64}", (("force", "1"),)),
]


@pytest.mark.parametrize(("method", "path", "query"), ALLOWED)
def test_the_verbs_a_run_needs_are_routed(
    method: str, path: str, query: tuple[tuple[str, str], ...]
) -> None:
    route(method, path, query)


CID = "b" * 64
REFUSED = [
    ("E7 list containers", "GET", "/containers/json", ()),
    ("E8 exec into a sandbox", "POST", f"/containers/{CID}/exec", ()),
    ("E9 attach to a sandbox", "POST", f"/containers/{CID}/attach", ()),
    ("E10 raise a sandbox's limits", "POST", f"/containers/{CID}/update", ()),
    ("E11 copy a file in", "PUT", f"/containers/{CID}/archive", ()),
    ("E11 copy a file out", "GET", f"/containers/{CID}/archive", ()),
    ("E12 stats", "GET", f"/containers/{CID}/stats", ()),
    ("E13 top", "GET", f"/containers/{CID}/top", ()),
    ("E14 restart", "POST", f"/containers/{CID}/restart", ()),
    ("E15 rename", "POST", f"/containers/{CID}/rename", ()),
    ("E16 commit", "POST", "/commit", ()),
    ("E18 pull an image", "POST", "/images/create", ()),
    ("E19 list images", "GET", "/images/json", ()),
    ("E20 create a network", "POST", "/networks/create", ()),
    ("E21 create a volume", "POST", "/volumes/create", ()),
    ("E22 daemon info", "GET", "/info", ()),
    ("E23 events", "GET", "/events", ()),
    ("E24 system df", "GET", "/system/df", ()),
    ("E25 build", "POST", "/build", ()),
    ("a session", "POST", "/session", ()),
    ("a plugin", "POST", "/plugins/pull", ()),
    ("a swarm service", "POST", "/services/create", ()),
    ("pause", "POST", f"/containers/{CID}/pause", ()),
    ("changes", "GET", f"/containers/{CID}/changes", ()),
    ("export", "GET", f"/containers/{CID}/export", ()),
    ("prune", "POST", "/containers/prune", ()),
    ("a container by name", "POST", "/containers/unicon-foreign/kill", ()),
    ("a container by short id", "GET", f"/containers/{CID[:12]}/json", ()),
    ("follow the logs", "GET", f"/containers/{CID}/logs", (("follow", "1"),)),
    ("delete a link", "DELETE", f"/containers/{CID}", (("link", "1"),)),
    (
        "start with detach keys",
        "POST",
        f"/containers/{CID}/start",
        (("detachKeys", "ctrl-a"),),
    ),
    (
        "create for another platform",
        "POST",
        "/containers/create",
        (("platform", "linux/arm64"),),
    ),
    (
        "create with a name another program on the machine means",
        "POST",
        "/containers/create",
        (("name", "wp_01j9example_0_grade"),),
    ),
    ("a version prefix the filter does not know", "GET", "/v1.4.5/version", ()),
    ("inspect with sizes", "GET", f"/containers/{CID}/json", (("size", "1"),)),
    ("ping with a body verb", "POST", "/_ping", ()),
]


@pytest.mark.parametrize(
    ("name", "method", "path", "query"), REFUSED, ids=[r[0] for r in REFUSED]
)
def test_everything_else_is_refused_with_a_reason(
    name: str, method: str, path: str, query: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(RefusedError) as refusal:
        route(method, path, query)
    assert refusal.value.reason
