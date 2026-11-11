"""Reading partitions back from a plan (``a.data.files()`` and the rest),
on fake machines: a storage device's partition reads the machine's image
file, a device memory's reads the bytes the test gives the machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.lang.library import PartitionHandle
from jig.testing import FakeMachine, FakeSimulation, run_source

from .storage_doubles import listing_image
from .test_partitions import PLUGIN, write_board

KIB = 1024
MEMBERS = """\
partition config: raw(on: flash0, at: 4 KiB, size: 4 KiB)
partition data: listing(on: flash0, at: 64 KiB, size: 64 KiB, block_size: 4 KiB)
partition table: raw(on: mcu.flash, at: 0x80000, size: 16 B)
"""


@pytest.fixture
def simulation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSimulation:
    """A project with the board ``store`` and a fake machine ``a`` whose
    flash image and internal flash a test filled."""
    root = tmp_path / "project"
    (root / "jig.toml").parent.mkdir(parents=True)
    (root / "jig.toml").write_text('[project]\nname = "p"\n', encoding="utf-8", newline="\n")
    write_board(root / "boards", MEMBERS, settings='plugin: "plugin.py"')
    (root / "boards" / "store" / "plugin.py").write_text(PLUGIN, encoding="utf-8", newline="\n")
    monkeypatch.setenv("JIG_PROJECT", str(root))
    fake = FakeSimulation(workdir=tmp_path / "out")

    def fill(machine: FakeMachine) -> None:
        image = bytearray(b"\xff" * 1024 * KIB)
        image[4 * KIB : 4 * KIB + 3] = b"CFG"
        files = {"a.txt": b"alpha", "sub/b.txt": b"beta"}
        image[64 * KIB : 128 * KIB] = listing_image(files, 64 * KIB, block_size=4096)
        machine.config.workdir.mkdir(parents=True, exist_ok=True)
        (machine.config.workdir / "flash0.img").write_bytes(bytes(image))
        flash = bytearray(b"\xff" * 0x200000)
        flash[0x80000:0x80010] = bytes(range(16))
        machine.memories["flash"] = bytes(flash)

    fake.on_board["a"] = fill
    return fake


def run(directory: Path, fake: FakeSimulation, tests: str) -> list[tuple[str, str, str]]:
    report = run_source(directory, 'system {\n    board a: "store"\n}\n' + tests, simulation=fake)
    return [(case.name, case.status, case.detail) for case in report.cases]


def test_a_plan_reads_a_partition_and_its_files(tmp_path: Path, simulation: FakeSimulation) -> None:
    results = run(
        tmp_path,
        simulation,
        'test "where it is" {\n'
        '    check a.config.name == "config" and a.config.format == "raw"\n'
        "    check a.config.at == 4 KiB and a.config.size == 4096\n"
        '    check a.data.format == "listing"\n'
        "}\n"
        'test "bytes" {\n'
        '    check a.config.bytes().startswith(x"434647ff")\n'
        "    check len(a.config.bytes()) == 4096\n"
        '    check a.table.bytes() == x"000102030405060708090a0b0c0d0e0f"\n'
        "}\n"
        'test "files" {\n'
        "    let top = a.data.files()\n"
        "    check len(top) == 2\n"
        '    check top[0].path == "/a.txt" and top[0].kind == "file" and top[0].size == 5\n'
        '    check top[1].name == "sub" and top[1].kind == "dir"\n'
        '    check a.data.files("/sub")[0].name == "b.txt"\n'
        "    let entries = a.data.walk()\n"
        '    check len(entries) == 3 and entries[1].path == "/sub"\n'
        '    check entries[2].path == "/sub/b.txt"\n'
        '    check text(a.data.read("/sub/b.txt")) == "beta"\n'
        '    check a.data.exists("/sub") and not a.data.exists("/c.txt")\n'
        '    check a.data.stat("/a.txt").size == 5\n'
        "}\n",
    )
    assert results == [
        ("where it is", "PASS", ""),
        ("bytes", "PASS", ""),
        ("files", "PASS", ""),
    ]


def test_reading_what_is_not_there_fails_the_step(
    tmp_path: Path, simulation: FakeSimulation
) -> None:
    results = run(
        tmp_path,
        simulation,
        'test "a missing file" {\n    let x = a.data.read("/c.txt")\n}\n'
        'test "a raw partition holds no files" {\n    let x = a.config.files()\n}\n'
        'test "a path is text" {\n    let x = a.data.read(3)\n}\n'
        'test "an unknown member" {\n    let x = a.data.erase\n}\n',
    )
    missing, raw, path, member = results
    assert missing[1] == "FAIL" and "a.data: /c.txt is not a file" in missing[2]
    assert raw[1] == "ERROR"
    assert "a.config is raw, a format without files: read its bytes()" in raw[2]
    assert path[1] == "ERROR" and 'a path in a partition is text, such as "/config' in path[2]
    assert member[1] == "ERROR" and "has no member 'erase' (has: at, bytes, exists" in member[2]


def test_a_partition_its_format_cannot_read_fails_the_step(
    tmp_path: Path, simulation: FakeSimulation
) -> None:
    def scramble(machine: FakeMachine) -> None:
        machine.config.workdir.mkdir(parents=True, exist_ok=True)
        (machine.config.workdir / "flash0.img").write_bytes(b"\x00" * 1024 * KIB)

    simulation.on_board["a"] = scramble
    ((_name, status, detail),) = run(
        tmp_path, simulation, 'test "t" {\n    check a.data.exists("/a.txt")\n}\n'
    )
    assert status == "FAIL"
    assert "a.data: listing cannot read it: no listing header" in detail


def test_a_partition_is_a_member_of_its_board(tmp_path: Path, simulation: FakeSimulation) -> None:
    run(tmp_path, simulation, 'test "t" { check true }\n')
    from jig.lang.library import BoardHandle

    handle = BoardHandle(simulation.machines["a"])  # type: ignore[arg-type]
    data = handle.dsl_attribute("data")
    assert isinstance(data, PartitionHandle) and repr(data) == "<partition a.data>"
    assert data.partition.target.name == "flash0"
    assert handle.dsl_attribute("nothing") is None
