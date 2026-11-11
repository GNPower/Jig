"""``jig image build``, ``jig image ls`` and ``jig image extract``: a
board's storage images written without Renode, and a partition's files
listed and copied out of an image."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.cli import main

from .test_partitions import PLUGIN, write_board

KIB = 1024
MEMBERS = """\
option data: "storage/full"
partition config: raw(on: flash0, at: 4 KiB, size: 4 KiB, contents: "storage/config.bin")
partition data: listing(on: flash0, at: 64 KiB, size: 64 KiB, block_size: 4 KiB,
                        contents: data)
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with the board ``store``: a raw partition and a listing
    partition filled from folders beside the board file."""
    root = tmp_path / "project"
    (root / "boards").mkdir(parents=True)
    (root / "jig.toml").write_text('[project]\nname = "p"\n', encoding="utf-8", newline="\n")
    write_board(root / "boards", MEMBERS, settings='plugin: "plugin.py"')
    board = root / "boards" / "store"
    (board / "plugin.py").write_text(PLUGIN, encoding="utf-8", newline="\n")
    storage = board / "storage"
    (storage / "full" / "logs").mkdir(parents=True)
    (storage / "full" / "settings.txt").write_bytes(b"rate=10\n")
    (storage / "full" / "logs" / "boot.log").write_bytes(b"ok\n")
    (storage / "empty").mkdir()
    (storage / "config.bin").write_bytes(b"CFG")
    monkeypatch.setenv("JIG_PROJECT", str(root))
    monkeypatch.chdir(tmp_path)
    return root


def test_image_build_writes_each_storage_devices_image(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["-q", "image", "build", "store", "-o", "images"]) == 0
    image = tmp_path / "images" / "flash0.img"
    data = image.read_bytes()
    assert len(data) == 1024 * KIB
    assert data[4 * KIB : 4 * KIB + 4] == b"CFG\xff"
    assert data[64 * KIB : 64 * KIB + 4] == b"LIST"
    out = capsys.readouterr().out
    assert out == (
        f"wrote {Path('images') / 'flash0.img'} (1048576 bytes, erased 0xFF; "
        "partitions config, data)\n"
    )
    assert main(["-q", "image", "build", "store", "-o", "zero", "--erased", "flash0=0"]) == 0
    zero = (tmp_path / "zero" / "flash0.img").read_bytes()
    assert zero[: 4 * KIB] == bytes(4 * KIB) and zero[4 * KIB : 4 * KIB + 4] == b"CFG\x00"
    assert main(["-q", "image", "build", "store", "--option", "data=storage/empty"]) == 0
    assert (tmp_path / "flash0.img").read_bytes()[64 * KIB : 64 * KIB + 12] == (
        b"LIST" + (4096).to_bytes(4, "little") + bytes(4)
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--erased", "flash9=0"], "--erased: board store has no storage flash9 (has: flash0)"),
        (["--erased", "flash0=256"], "--erased expects STORAGE=BYTE, such as flash0=0x00"),
        (["--boot", "qspi"], "board store has no boot mode 'qspi' (has: flash)"),
    ],
)
def test_image_build_names_a_bad_argument(
    project: Path, arguments: list[str], message: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert main(["-q", "image", "build", "store", *arguments]) == 2
    assert message in caplog.text


def test_image_build_reports_a_board_without_storage(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = project / "boards" / "store" / "board.jig"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("storage: [flash0]", "").split("option data")[0]
        + "    link spare: spi(controller: mcu.spi2, targets: {0: flash1})\n"
        '    peripheral flash1: "JigFixtures.SpiNorFlash" (size: 1 MiB)\n}\n',
        encoding="utf-8",
        newline="\n",
    )
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("    link spi: spi(controller: mcu.spi1, targets: {0: flash0})\n", "").replace(
            '    peripheral flash0: "JigFixtures.SpiNorFlash" (size: 1 MiB)\n', ""
        ),
        encoding="utf-8",
        newline="\n",
    )
    assert main(["-q", "image", "build", "store"]) == 0
    assert capsys.readouterr().out == "board store has no storage devices\n"


def test_image_ls_and_extract_read_a_partition_of_an_image(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["-q", "image", "build", "store", "-o", "images"]) == 0
    capsys.readouterr()
    image = str(tmp_path / "images" / "flash0.img")
    common = ["--board", "store", "--partition", "data"]
    assert main(["-q", "image", "ls", image, *common]) == 0
    assert capsys.readouterr().out == ("dir              /logs\nfile          8  /settings.txt\n")
    assert main(["-q", "image", "ls", image, *common, "--path", "/logs"]) == 0
    assert capsys.readouterr().out == "file          3  /logs/boot.log\n"
    assert main(["-q", "image", "extract", image, *common, "-o", "files"]) == 0
    assert (tmp_path / "files" / "logs" / "boot.log").read_bytes() == b"ok\n"
    assert (tmp_path / "files" / "settings.txt").read_bytes() == b"rate=10\n"
    assert "extracted 2 files and 1 directories to" in capsys.readouterr().out
    raw = ["--board", "store", "--partition", "config"]
    assert main(["-q", "image", "extract", image, *raw, "-o", "raw"]) == 0
    assert (tmp_path / "raw" / "config.bin").read_bytes() == b"CFG" + b"\xff" * (4 * KIB - 3)
    assert main(["-q", "image", "ls", image, *raw]) == 2


def test_image_ls_names_what_it_cannot_read(
    project: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    short = tmp_path / "short.img"
    short.write_bytes(b"\xff" * 1024)
    common = ["--board", "store", "--partition", "data"]
    assert main(["-q", "image", "ls", str(short), *common]) == 2
    assert "partition data ends at 0x20000" in caplog.text
    assert main(["-q", "image", "ls", str(tmp_path / "none.img"), *common]) == 2
    assert "partition data: no image" in caplog.text
    assert main(["-q", "image", "ls", str(short), "--board", "store", "--partition", "x"]) == 2
    assert "board store has no partition 'x' (has: config, data)" in caplog.text


def test_image_extract_keeps_every_file_inside_its_directory(
    project: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import struct

    from .storage_doubles import MAGIC

    up, name = b"/..", b"/../escaped.txt"
    listing = MAGIC + struct.pack("<II", 4096, 2)
    listing += struct.pack("<HBI", len(up), 1, 0) + up
    listing += struct.pack("<HBI", len(name), 0, 2) + name + b"hi"
    image = bytearray(b"\xff" * 1024 * KIB)
    image[64 * KIB : 64 * KIB + len(listing)] = listing
    path = tmp_path / "crafted.img"
    path.write_bytes(bytes(image))
    common = ["--board", "store", "--partition", "data", "-o", str(tmp_path / "out" / "files")]
    assert main(["-q", "image", "extract", str(path), *common]) == 2
    assert "partition data: /.. leads outside" in caplog.text
    assert not (tmp_path / "out" / "escaped.txt").exists()


def test_image_build_takes_a_storage_map(
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "a.txt").write_bytes(b"from the map\n")
    (tmp_path / "cfg2.bin").write_bytes(b"CF2")
    storage = ["--storage", "data=other", "--storage", "config=cfg2.bin"]
    assert main(["-q", "image", "build", "store", "-o", "mapped", *storage]) == 0
    image = tmp_path / "mapped" / "flash0.img"
    assert image.read_bytes()[4 * KIB : 4 * KIB + 4] == b"CF2\xff"
    capsys.readouterr()
    common = ["--board", "store", "--partition", "data"]
    assert main(["-q", "image", "ls", str(image), *common]) == 0
    assert capsys.readouterr().out == "file         13  /a.txt\n"
    assert main(["-q", "image", "build", "store", "--storage", "data"]) == 2
    assert "--storage expects NAME=PATH, got 'data'" in caplog.text
    assert main(["-q", "image", "build", "store", "--storage", "nope=cfg2.bin"]) == 2
    assert "nope" in caplog.text


def test_image_ls_lists_recursively_and_takes_a_path_without_its_slash(
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert main(["-q", "image", "build", "store", "-o", "images"]) == 0
    capsys.readouterr()
    image = str(tmp_path / "images" / "flash0.img")
    common = ["-q", "image", "ls", image, "--board", "store", "--partition", "data"]
    assert main([*common, "-r"]) == 0
    assert capsys.readouterr().out == (
        "dir              /logs\nfile          3  /logs/boot.log\nfile          8  /settings.txt\n"
    )
    for path in (["--path", "logs"], ["--path", "/logs/", "--recursive"]):
        assert main([*common, *path]) == 0
        assert capsys.readouterr().out == "file          3  /logs/boot.log\n"
    assert main([*common, "--path", "settings.txt", "-r"]) == 2
    assert "partition data: /settings.txt is not a directory" in caplog.text
