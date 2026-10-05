"""A stand-in harness for the filter's Docker integration test, run in a
container the way the harness is: UNICON_GRADING_ID set, the run's workspace at
/woodpecker, the filter's socket at /run/unicon/docker.sock. It speaks raw HTTP,
so it can send what the docker CLI cannot, and prints one JSON line per check:
{"check": ..., "status": ..., "detail": ...}.

`python standin.py checks` runs the harness's own path, the escape probe and
every request the filter must refuse. `python standin.py hold <time_ms>`
creates and starts a sleeping step, prints its id and waits, for the reaper
and cross-run checks. `python standin.py poke <id>` tries another run's step.
`python standin.py replace` removes the filter's socket and binds its own in
its place, for the check that the filter notices. Standard library only.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

SOCKET = "/run/unicon/docker.sock"
BASE = "/v1.45"
MIB = 1024 * 1024

PROBE = """
import os, socket
status = open("/proc/self/status").read()
fields = dict(l.split(":", 1) for l in status.splitlines() if ":" in l)
found = {
    "uid": os.getuid(),
    "cap_eff": fields["CapEff"].strip(),
    "seccomp": fields["Seccomp"].strip(),
    "no_new_privs": fields["NoNewPrivs"].strip(),
    "docker_socket": os.path.exists("/var/run/docker.sock"),
}
try:
    os.unshare(os.CLONE_NEWUSER); found["user_namespace"] = "created"
except OSError:
    found["user_namespace"] = "blocked"
for name, path in (
    ("write_root", "/escape"),
    ("write_etc", "/etc/passwd"),
    ("write_work", "/work/probe-wrote"),
    ("write_tmp", "/tmp/probe-wrote"),
):
    try:
        open(path, "a").close(); found[name] = "ok"
    except OSError as exc:
        found[name] = exc.strerror
try:
    socket.create_connection(("1.1.1.1", 80), timeout=2); found["network"] = "open"
except OSError:
    found["network"] = "blocked"
found["interfaces"] = sorted(os.listdir("/sys/class/net"))
print("PROBE " + __import__("json").dumps(found))
"""


def exchange(raw: bytes, count: int = 1) -> list[tuple[int, bytes]]:
    """Send raw bytes on one connection and read up to `count` answers."""
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(SOCKET)
    sock.sendall(raw)
    reader = sock.makefile("rb")
    answers = []
    for _ in range(count):
        status_line = reader.readline()
        if not status_line:
            break
        headers = {}
        while (line := reader.readline()) not in (b"\r\n", b""):
            name, _, value = line.decode().partition(":")
            headers[name.lower()] = value.strip()
        body = reader.read(int(headers.get("content-length", "0")))
        answers.append((int(status_line.split()[1]), body))
    sock.close()
    return answers


def request(method: str, path: str, document: Any = None) -> bytes:
    data = b"" if document is None else json.dumps(document).encode()
    head = f"{method} {path} HTTP/1.1\r\nHost: docker\r\n"
    if data or method == "POST":
        head += f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
    return (head + "\r\n").encode() + data


def call(
    method: str, path: str, document: Any = None, query: str = ""
) -> tuple[int, Any]:
    found = exchange(request(method, BASE + path + query, document))
    if not found:
        return 0, None
    status, body = found[0]
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body.decode(errors="replace")


def report(check: str, passed: bool, detail: Any = "") -> None:
    print(json.dumps({"check": check, "passed": passed, "detail": detail}), flush=True)


def step_dir(name: str) -> str:
    path = f"/woodpecker/unicon-steps/{name}"
    Path(path).mkdir(parents=True, exist_ok=True)
    os.chown("/woodpecker/unicon-steps", 10001, 10001)
    os.chown(path, 10001, 10001)
    return f"unicon-steps/{name}"


def sandbox(command: list[str], time_ms: int = 60000) -> dict[str, Any]:
    return {
        "Image": os.environ["IMAGE"],
        "Entrypoint": command,
        "User": "10001:10001",
        "WorkingDir": "/work",
        "Labels": {
            "unicon.grading": os.environ["UNICON_GRADING_ID"],
            "unicon.time_ms": str(time_ms),
        },
        "HostConfig": {
            "NetworkMode": "none",
            "Init": False,
            "IpcMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges", "seccomp=builtin"],
            "Memory": 128 * MIB,
            "MemorySwap": 128 * MIB,
            "NanoCpus": 1_000_000_000,
            "PidsLimit": 64,
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": os.environ["WORKSPACE"],
                    "Target": "/work",
                    "VolumeOptions": {"Subpath": step_dir("probe"), "NoCopy": True},
                },
                {
                    "Type": "tmpfs",
                    "Target": "/tmp",
                    "TmpfsOptions": {"SizeBytes": 16 * MIB},
                },
            ],
            "LogConfig": {
                "Type": "json-file",
                "Config": {"max-size": "8m", "max-file": "1"},
            },
        },
    }


def own_id() -> str:
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        if "/containers/" in line and "/hostname " in line:
            return line.split("/containers/")[1].split("/")[0]
    raise SystemExit("not in a container")


def expect(check: str, status: int, wanted: int, body: Any) -> None:
    detail = body.get("message", "") if isinstance(body, dict) else str(body)[:160]
    report(check, status == wanted, f"{status} {detail}")


def replace_socket() -> None:
    """Put a socket of this program's own in the filter's place, as a harness
    running as root with the socket's directory writable could, and wait.
    """
    Path(SOCKET).unlink()
    impostor = socket.socket(socket.AF_UNIX)
    impostor.bind(SOCKET)
    impostor.listen()
    print(json.dumps({"replaced": SOCKET}), flush=True)
    time.sleep(600)


def checks() -> None:
    status, body = call("GET", "/version")
    expect("A0 ping and version", status, 200, body)
    # The filter's uid as this container sees it: 10002, or under
    # userns-remap the overflow uid, since the filter runs outside the remap
    # and its uid is not mapped into this container's.
    held = Path(SOCKET).stat()
    owner = int(os.environ["SOCKET_OWNER"])
    report(
        "S the socket belongs to the filter's uid, not the harness's",
        held.st_uid == owner and held.st_uid != os.getuid(),
        {"uid": held.st_uid, "expected": owner, "mode": oct(held.st_mode & 0o777)},
    )
    try:
        Path(SOCKET).unlink()
        removed = "removed"
    except OSError as exc:
        removed = exc.strerror or "refused"
    report(
        "S a harness cannot remove the filter's socket", removed != "removed", removed
    )
    status, body = call("GET", f"/containers/{own_id()}/json")
    expect("A self-inspect finds the workspace", status, 200, "")

    status, body = call("POST", "/containers/create", sandbox(["python", "-c", PROBE]))
    expect("A1 create a sandboxed step", status, 201, body)
    step = body["Id"] if status == 201 else "0" * 64
    for check, method, path, wanted in (
        ("A2 start it", "POST", f"/containers/{step}/start", 204),
        ("A3 wait for it", "POST", f"/containers/{step}/wait", 200),
        ("A4 read its log", "GET", f"/containers/{step}/logs?stdout=1&stderr=1", 200),
        ("A5 inspect it", "GET", f"/containers/{step}/json", 200),
    ):
        status, answer = call(method, path)
        expect(check, status, wanted, "" if status == wanted else answer)
        if check.startswith("A4"):
            text = answer if isinstance(answer, str) else ""
            probe = (
                text[text.find("PROBE ") + 6 :].splitlines()[0]
                if "PROBE " in text
                else "{}"
            )
            report("B the escape probe", True, json.loads(probe))
        if check.startswith("A5") and isinstance(answer, dict):
            labels = answer["Config"]["Labels"]
            report(
                "A8 the filter labelled the step with its harness",
                labels.get("unicon.harness") == own_id(),
                labels,
            )
            host = answer["HostConfig"]
            report(
                "A5 the flags as the daemon keeps them",
                True,
                {
                    "CapDrop": host["CapDrop"],
                    "SecurityOpt": host["SecurityOpt"],
                    "ReadonlyRootfs": host["ReadonlyRootfs"],
                    "NetworkMode": host["NetworkMode"],
                    "Memory": host["Memory"],
                    "MemorySwap": host["MemorySwap"],
                    "PidsLimit": host["PidsLimit"],
                    "User": answer["Config"]["User"],
                },
            )
    wrote = Path("/woodpecker/unicon-steps/probe/probe-wrote").exists()
    report("A7 the step wrote into its own directory of the workspace", wrote)
    status, body = call("DELETE", f"/containers/{step}?force=1")
    expect("A6 remove it", status, 204, body)

    base = sandbox(["true"])

    def mutated(change: Any) -> dict[str, Any]:
        document = copy.deepcopy(base)
        change(document)
        return document

    def in_host(**fields: Any) -> Any:
        return lambda d: d["HostConfig"].update(fields)

    def add_mount(extra: dict[str, Any]) -> Any:
        return lambda d: d["HostConfig"]["Mounts"].append(extra)

    refused = [
        ("C1 privileged", in_host(Privileged=True)),
        ("C2 bind the host root", in_host(Binds=["/:/host"])),
        (
            "C3 bind the real docker socket",
            add_mount(
                {"Type": "bind", "Source": "/var/run/docker.sock", "Target": "/s"}
            ),
        ),
        (
            "C4 mount the filter's socket directory",
            add_mount(
                {
                    "Type": "bind",
                    "Source": os.environ["FILTER_DIRECTORY"],
                    "Target": "/p",
                }
            ),
        ),
        ("C5 host network", in_host(NetworkMode="host")),
        ("C6 bridge network", in_host(NetworkMode="bridge")),
        ("C7 cap-add SYS_ADMIN", in_host(CapAdd=["SYS_ADMIN"])),
        (
            "C8 seccomp unconfined",
            in_host(SecurityOpt=["no-new-privileges", "seccomp=unconfined"]),
        ),
        ("C9 host pid namespace", in_host(PidMode="host")),
        ("C10 host ipc namespace", in_host(IpcMode="host")),
        (
            "C11 a device",
            in_host(
                Devices=[
                    {
                        "PathOnHost": "/dev/null",
                        "PathInContainer": "/dev/x",
                        "CgroupPermissions": "rwm",
                    }
                ]
            ),
        ),
        ("C12 an image not on the list", lambda d: d.update(Image="alpine:latest")),
        ("C14 a restart policy", in_host(RestartPolicy={"Name": "always"})),
        (
            "C15 a published port",
            in_host(PortBindings={"80/tcp": [{"HostPort": "18080"}]}),
        ),
        ("C16 a sysctl", in_host(Sysctls={"net.ipv4.ip_unprivileged_port_start": "0"})),
        ("C17 userns host", in_host(UsernsMode="host")),
        ("C18 a cgroup parent", in_host(CgroupParent="/unicon-escape")),
        ("D1 without cap-drop ALL", in_host(CapDrop=None)),
        ("D2 without read-only root", in_host(ReadonlyRootfs=False)),
        ("D3 without seccomp named", in_host(SecurityOpt=["no-new-privileges"])),
        ("D4 without a memory limit", in_host(Memory=0, MemorySwap=0)),
        ("D5 without a pids limit", in_host(PidsLimit=0)),
        ("D7 as root", lambda d: d.update(User="0:0")),
        ("D8 without the run label", lambda d: d["Labels"].pop("unicon.grading")),
        ("D9 with swap", in_host(MemorySwap=256 * MIB)),
        ("D10 on the default network", in_host(NetworkMode="default")),
        (
            "X another grading's label",
            lambda d: d["Labels"].update(
                {"unicon.grading": "0199a2c1-0000-7c3a-9f10-000000000000"}
            ),
        ),
        (
            "X another run's workspace",
            lambda d: d["HostConfig"]["Mounts"][0].update(
                Source=os.environ["OTHER_WORKSPACE"]
            ),
        ),
        (
            "X the whole workspace with the checkouts",
            lambda d: d["HostConfig"]["Mounts"][0].update(VolumeOptions={}),
        ),
        ("R masked paths emptied", in_host(MaskedPaths=[])),
        ("R readonly paths emptied", in_host(ReadonlyPaths=[])),
        ("R the legacy Capabilities field", in_host(Capabilities=["CAP_SYS_ADMIN"])),
        ("R another runtime", in_host(Runtime="runc-unconfined")),
        ("R an anonymous volume", lambda d: d.update(Volumes={"/data": {}})),
        ("R a field in another case", in_host(privileged=True)),
        ("R a bind given as empty text", in_host(Binds=[""])),
        (
            "R a label another program goes by",
            lambda d: d["Labels"].update({"com.docker.compose.project": "unicon"}),
        ),
        (
            "R the harness label set by the harness",
            lambda d: d["Labels"].update({"unicon.harness": "0" * 64}),
        ),
    ]
    for check, change in refused:
        status, body = call("POST", "/containers/create", mutated(change))
        expect(check, status, 403, body)
        if status == 201:
            call("DELETE", f"/containers/{body['Id']}?force=1")

    foreign = os.environ["FOREIGN"]
    for check, method, path in (
        (
            "E1 inspect a container that is not a step",
            "GET",
            f"/containers/{foreign}/json",
        ),
        ("E2 kill it", "POST", f"/containers/{foreign}/kill"),
        ("E3 remove it", "DELETE", f"/containers/{foreign}?force=1"),
        ("E4 start it", "POST", f"/containers/{foreign}/start"),
        ("E5 wait on it", "POST", f"/containers/{foreign}/wait"),
        ("E6 read its log", "GET", f"/containers/{foreign}/logs?stdout=1"),
        ("E7 list containers", "GET", "/containers/json?all=1"),
        ("E8 exec", "POST", f"/containers/{foreign}/exec"),
        ("E9 attach", "POST", f"/containers/{foreign}/attach?stream=1"),
        ("E10 raise limits", "POST", f"/containers/{foreign}/update"),
        ("E11 copy a file", "PUT", f"/containers/{foreign}/archive?path=/tmp"),
        ("E12 stats", "GET", f"/containers/{foreign}/stats"),
        ("E13 top", "GET", f"/containers/{foreign}/top"),
        ("E14 restart", "POST", f"/containers/{foreign}/restart"),
        ("E15 rename", "POST", f"/containers/{foreign}/rename?name=x"),
        ("E16 commit", "POST", f"/commit?container={foreign}"),
        ("E17 a container by name", "POST", "/containers/unicon-foreign/kill"),
        ("E18 pull", "POST", "/images/create?fromImage=busybox"),
        ("E19 list images", "GET", "/images/json"),
        ("E20 create a network", "POST", "/networks/create"),
        ("E21 create a volume", "POST", "/volumes/create"),
        ("E22 daemon info", "GET", "/info"),
        ("E23 events", "GET", "/events"),
        ("E24 system df", "GET", "/system/df"),
        ("E25 build", "POST", "/build"),
    ):
        status, body = call(method, path)
        expect(check, status, 403, body)

    chunked = (
        f"POST {BASE}/containers/create HTTP/1.1\r\nHost: docker\r\n"
        "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
    ).encode()
    data = json.dumps(base).encode()
    chunked += f"{len(data):x}\r\n".encode() + data + b"\r\n0\r\n\r\n"
    found = exchange(chunked)
    report("R a chunked body", found[0][0] == 403, found[0][1].decode())
    found = exchange(
        request("GET", "/_ping")
        + request("GET", "/containers/json")
        + request("GET", "/_ping"),
        count=3,
    )
    statuses = [s for s, _ in found]
    report(
        "K1 a request smuggled after an allowed one on a kept-alive connection",
        statuses == [200, 403],
        statuses,
    )
    after = sequential()
    report(
        "K2 a forbidden request sent after the allowed one's answer was read",
        after == [200, 403],
        after,
    )
    status, body = call(
        "POST", "/containers/create", sandbox(["true"]), query="?name=unicon-proxy-1"
    )
    expect("R a create naming its container", status, 403, body)


def sequential() -> list[int]:
    """Ping, read its answer, then list containers on the same connection."""
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(SOCKET)
    reader = sock.makefile("rb")
    statuses = []
    for raw in (
        request("GET", BASE + "/_ping"),
        request("GET", BASE + "/containers/json"),
    ):
        sock.sendall(raw)
        status_line = reader.readline()
        if not status_line:
            break
        headers = {}
        while (line := reader.readline()) not in (b"\r\n", b""):
            name, _, value = line.decode().partition(":")
            headers[name.lower()] = value.strip()
        reader.read(int(headers.get("content-length", "0")))
        statuses.append(int(status_line.split()[1]))
    sock.close()
    return statuses


def hold(time_ms: int) -> None:
    status, body = call(
        "POST", "/containers/create", sandbox(["sleep", "600"], time_ms)
    )
    if status != 201:
        raise SystemExit(f"create answered {status}: {body}")
    call("POST", f"/containers/{body['Id']}/start")
    print(json.dumps({"held": body["Id"]}), flush=True)
    time.sleep(600)


def poke(container: str) -> None:
    for check, method, path in (
        ("X inspect another run's step", "GET", f"/containers/{container}/json"),
        ("X kill another run's step", "POST", f"/containers/{container}/kill"),
        ("X remove another run's step", "DELETE", f"/containers/{container}?force=1"),
    ):
        status, body = call(method, path)
        expect(check, status, 403, body)


if __name__ == "__main__":
    if sys.argv[1] == "checks":
        checks()
    elif sys.argv[1] == "hold":
        hold(int(sys.argv[2]))
    elif sys.argv[1] == "replace":
        replace_socket()
    else:
        poke(sys.argv[2])
