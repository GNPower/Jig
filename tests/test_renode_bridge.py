"""The Renode layer on a real Renode: the bridge, model sets, search paths, and the commands
that start Renode (``jig doctor``, ``jig models check``).

Run with ``pytest -m renode`` (``JIG_RENODE`` or a Renode that discovery finds).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jig import project
from jig.errors import RenodeError
from jig.renode import commands, models
from jig.renode.discover import RenodeInstall
from jig.renode.models import ModelCompileError, ModelSet
from jig.renode.process import RenodeProcess
from jig.renode.session import RenodeSession

from .renode_support import SMALLEST_ZYNQ_OVERLAY, real_renode

pytestmark = pytest.mark.renode

PROBE_A = """\
using Antmicro.Renode.Peripherals.Jig.Harness;

namespace Antmicro.Renode.Peripherals.Jig.Tests
{
    public static class ProbeA
    {
        // Names a Jig type: this set compiles against Jig's models.
        public static string Port => typeof(UartPort).Name;

        public static uint Value => 41;
    }
}
"""

PROBE_B = """\
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.Tests
{
    public static class ProbeExtensions
    {
        // Uses set A's type: this set compiles after it.
        public static uint JigTestProbe(this Emulation emulation)
        {
            return ProbeA.Value + 1;
        }
    }

    public class ProbePeripheral : IDoubleWordPeripheral
    {
        public uint ReadDoubleWord(long offset)
        {
            return offset == 0 ? 0x4A494701u : ProbeA.Value;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
        }

        public void Reset()
        {
        }
    }
}
"""

BROKEN = """\
namespace Antmicro.Renode.Peripherals.Jig.Tests
{
    public static class Broken
    {
        public static int Value()
        {
            return 1
        }
    }
}
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _exchange(sock: socket.socket, request: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(request).encode()
    sock.sendall(struct.pack(">I", len(body)) + body)
    header = sock.recv(4)
    assert len(header) == 4, "the bridge closed the connection"
    (length,) = struct.unpack(">I", header)
    data = b""
    while len(data) < length:
        data += sock.recv(length - len(data))
    reply: dict[str, Any] = json.loads(data)
    return reply


def _closed(sock: socket.socket) -> bool:
    sock.settimeout(10)
    try:
        return sock.recv(1) == b""
    except ConnectionResetError:
        return True


@pytest.fixture(scope="module")
def renode() -> RenodeInstall:
    return real_renode()


@pytest.fixture(scope="module")
def sets(tmp_path_factory: pytest.TempPathFactory) -> tuple[ModelSet, ModelSet]:
    root = tmp_path_factory.mktemp("model-sets")
    a = ModelSet("probe-a", root / "a", (_write(root / "a" / "ProbeA.cs", PROBE_A),))
    b = ModelSet("probe-b", root / "b", (_write(root / "b" / "ProbeB.cs", PROBE_B),))
    return a, b


@pytest.fixture(scope="module")
def session(
    renode: RenodeInstall,
    sets: tuple[ModelSet, ModelSet],
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[RenodeSession]:
    workdir = tmp_path_factory.mktemp("bridge") / "renode"
    a, b = sets
    duplicate = ModelSet("probe-a-again", a.root, a.files)
    with RenodeSession(
        workdir=workdir, install=renode, model_sets=(a, b, duplicate), step_wall_limit=120.0
    ) as running:
        yield running


def test_a_session_runs_one_second_on_a_zynq_machine(
    session: RenodeSession, tmp_path: Path
) -> None:
    overlay = _write(
        tmp_path / "zynq.repl",
        f'using "{project.platform_file("xilinx/zynq7000").as_posix()}"\n\n{SMALLEST_ZYNQ_OVERLAY}',
    )
    session.create_machine("zynq")
    session.execute(f"machine LoadPlatformDescription @{overlay.as_posix()}")
    session.execute("bootrom PowerOn")
    started = session.elapsed_ns
    wall = time.monotonic()
    for _ in range(100):
        session.step_and_collect(10_000_000)
    assert session.elapsed_ns - started == 1_000_000_000
    reported = session.execute("emulation ElapsedNanoseconds").strip().splitlines()[0]
    assert int(reported, 0) == session.elapsed_ns
    assert time.monotonic() - wall < 60
    record = json.loads((session.workdir / "session.json").read_text(encoding="utf-8"))
    assert record["bridge"]["protocol"] == 1
    assert record["bridge"]["version"].startswith("Renode, version")
    assert [s["name"] for s in record["model_sets"]] == ["jig", "probe-a", "probe-b"]


def test_model_sets_compile_in_order_against_jig(session: RenodeSession) -> None:
    assert [t.name for t in session.model_timings] == ["jig", "probe-a", "probe-b"]
    assert int(session.execute("emulation JigTestProbe").strip(), 0) == 42
    session.create_machine("probe")
    session.execute(
        'machine LoadPlatformDescriptionFromString "probe: Jig.Tests.ProbePeripheral @ sysbus '
        '<0x70000000, +0x100>"'
    )
    assert int(session.execute("sysbus ReadDoubleWord 0x70000000").strip(), 0) == 0x4A494701
    assert int(session.execute("sysbus ReadDoubleWord 0x70000004").strip(), 0) == 41


def test_the_monitor_reports_the_protocol_version(session: RenodeSession) -> None:
    assert int(session.execute("emulation JigProtocolVersion").strip(), 0) == 1
    assert session.python("1 + 1") == "2"


def test_the_bridge_refuses_a_wrong_token(session: RenodeSession) -> None:
    info = session.process.info
    with socket.create_connection(("127.0.0.1", info.port), timeout=10) as raw:
        reply = _exchange(raw, {"id": 7, "op": "command", "token": "0" * 64, "command": "version"})
        assert reply == {"id": 7, "ok": False, "kind": "auth", "error": "invalid token"}
        assert _closed(raw)
    with socket.create_connection(("127.0.0.1", info.port), timeout=10) as raw:
        reply = _exchange(raw, {"id": 1, "op": "hello"})
        assert reply["kind"] == "auth"
    with socket.create_connection(("127.0.0.1", info.port), timeout=10) as raw:
        raw.sendall(struct.pack(">I", 1 << 20))  # a frame too large to precede a token
        assert _closed(raw)
    with socket.create_connection(("127.0.0.1", info.port), timeout=10) as raw:
        reply = _exchange(raw, {"id": 1, "op": "hello", "token": info.token})
        assert reply["kind"] == "busy"  # one client at a time
    assert session.execute("version").startswith("Renode, version 1.")


def _local_addresses() -> set[str]:
    addresses: set[str] = set()
    with contextlib.suppress(OSError):
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(str(entry[4][0]))
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.connect(("192.0.2.1", 9))  # TEST-NET-1; connecting a UDP socket sends nothing
        addresses.add(str(udp.getsockname()[0]))
    return {address for address in addresses if not address.startswith("127.")}


def _listening(port: int) -> list[str]:
    """The local addresses that listen on TCP ``port``."""
    if os.name == "nt":
        found = []
        for family in ("TCP", "TCPv6"):
            text = subprocess.run(
                ["netstat", "-ano", "-p", family], capture_output=True, text=True, check=True
            ).stdout
            for line in text.splitlines():
                fields = line.split()
                if len(fields) >= 4 and fields[3] == "LISTENING":
                    address, _, listen_port = fields[1].rpartition(":")
                    if listen_port == str(port):
                        found.append(address)
        return found
    found = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        with contextlib.suppress(FileNotFoundError):
            for line in Path(table).read_text(encoding="ascii").splitlines()[1:]:
                fields = line.split()
                address, _, hex_port = fields[1].partition(":")
                if fields[3] == "0A" and int(hex_port, 16) == port:
                    found.append(address)
    return found


def test_the_bridge_listens_on_loopback_only(session: RenodeSession) -> None:
    port = session.process.info.port
    listening = _listening(port)
    assert listening in (["127.0.0.1"], ["0100007F"]), listening
    for address in sorted(_local_addresses()):
        with pytest.raises(OSError), socket.create_connection((address, port), timeout=3):
            pass
    assert session.execute("version").startswith("Renode")


def test_renode_and_jig_platform_paths_resolve(session: RenodeSession, tmp_path: Path) -> None:
    # A board repl names Renode's platforms relative to Renode's root, and
    # Jig's platforms relative to Jig's platforms directory.
    stm = _write(tmp_path / "board" / "stm.repl", 'using "platforms/cpus/stm32f4.repl"\n')
    session.create_machine("stm")
    session.execute(f"machine LoadPlatformDescription @{stm.as_posix()}")
    assert "usart1" in session.execute("peripherals")
    zynq = _write(
        tmp_path / "board" / "zynq.repl", f'using "xilinx/zynq7000.repl"\n\n{SMALLEST_ZYNQ_OVERLAY}'
    )
    session.create_machine("zynq_by_name")
    session.execute(f"machine LoadPlatformDescription @{zynq.as_posix()}")
    assert "slcr" in session.execute("peripherals")


def test_a_broken_model_reports_its_file_and_line(renode: RenodeInstall, tmp_path: Path) -> None:
    root = tmp_path / "broken-board"
    broken = ModelSet("broken", root, (_write(root / "models" / "Broken.cs", BROKEN),))
    session = RenodeSession(workdir=tmp_path / "renode", install=renode, model_sets=(broken,))
    with pytest.raises(ModelCompileError) as caught:
        session.start()
    error = caught.value
    assert error.set_name == "broken"
    first = error.diagnostics[0]
    assert (first.set, first.file, first.line, first.code) == (
        "broken",
        "models/Broken.cs",
        7,
        "CS1002",
    )
    assert "broken: models/Broken.cs(7," in str(error)
    with pytest.raises(RenodeError, match="Renode is not running"):  # stopped, not left behind
        session.execute("version")


def test_renode_exits_when_its_client_disconnects(renode: RenodeInstall, tmp_path: Path) -> None:
    process = RenodeProcess(workdir=tmp_path / "renode", install=renode)
    process.start()
    try:
        assert process.startup_seconds is not None and process.startup_seconds > 0
        process.client.close()
        assert process.wait(timeout=30) == 0
        assert "the client disconnected" in process.log_path.read_text(encoding="utf-8")
    finally:
        process.stop()


def test_two_renodes_start_side_by_side(renode: RenodeInstall, tmp_path: Path) -> None:
    first = RenodeProcess(workdir=tmp_path / "first", install=renode)
    second = RenodeProcess(workdir=tmp_path / "second", install=renode)
    errors: list[BaseException] = []

    def start(process: RenodeProcess) -> None:
        try:
            process.start()
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(p,)) for p in (first, second)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert first.info.port != second.info.port
        assert first.info.token != second.info.token
        for process in (first, second):
            assert process.client.execute("version").startswith("Renode, version 1.")
    finally:
        first.stop()
        second.stop()


def test_doctor_json_passes(
    renode: RenodeInstall, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    board = tmp_path / "board"
    _write(board / "models" / "ProbeA.cs", PROBE_A)
    code = commands.main(
        ["doctor", "--json", "--renode", str(renode.executable), "--board", str(board)]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0, json.dumps(report, indent=2)
    assert report["ok"]
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["start-up"]["status"] == "pass"
    assert checks["start-up"]["details"]["address"].startswith("127.0.0.1:")
    compiled = checks["models"]["details"]["compiled"]
    assert [c["set"] for c in compiled] == ["jig", "board"]
    assert all(c["seconds"] > 0 for c in compiled)


def test_models_check_compiles_a_board_in_a_throwaway_renode(
    renode: RenodeInstall, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good"
    _write(good / "models" / "ProbeA.cs", PROBE_A)
    bad = tmp_path / "bad"
    _write(bad / "models" / "Broken.cs", BROKEN)
    assert (
        commands.main(["models", "check", "--renode", str(renode.executable), "--board", str(good)])
        == 0
    )
    assert "ok    good (1 files)" in capsys.readouterr().out
    assert (
        commands.main(["models", "check", "--renode", str(renode.executable), "--board", str(bad)])
        == 1
    )
    out = capsys.readouterr().out
    assert "FAIL  bad" in out and "bad: Broken.cs(7," in out


def test_models_check_with_the_sdk(
    renode: RenodeInstall, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if models.dotnet() is None or not renode.has_assemblies:
        pytest.skip("needs the .NET SDK and Renode's assemblies")
    good = tmp_path / "good"
    _write(good / "models" / "ProbeA.cs", PROBE_A)
    code = commands.main(
        ["models", "check", "--sdk", "--renode", str(renode.executable), "--board", str(good)]
    )
    assert code == 0, capsys.readouterr().out
    assert "SDK build, warnings as errors" in capsys.readouterr().out
