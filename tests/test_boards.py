"""Board definitions: the search path, board.yaml schema 2, model sets and
the CSP extension's section, on the fixture project."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from jig.errors import ConfigError
from jig.ext.csp.settings import CspSettings
from jig.system import boards

from .conftest import FIXTURES

BOARDS = FIXTURES / "boards"
EXTRA = FIXTURES / "extra" / "boards"
SHADOW = FIXTURES / "shadow" / "boards"


def board_file(directory: Path, text: str, *files: str) -> Path:
    """A board.yaml in ``directory`` (plus empty ``files`` beside it)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in files:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    path = directory / "board.yaml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


MINIMAL = "schema: 2\nname: x\nsoc: stm32\nplatform: x.repl\n"


# ---------------------------------------------------------------- search path


def test_the_project_names_the_board_roots() -> None:
    (root,) = boards.search_path()
    assert root == boards.BoardRoot(BOARDS, "jig.toml [paths] boards")
    assert boards.available() == [
        "example-node",
        "example-package",
        "example-stm32",
        "example-zynq",
    ]
    found = boards.catalog()["example-node"]
    assert found.path == BOARDS / "example-node" / "board.yaml"
    assert found.origin == "jig.toml [paths] boards"


def test_jig_board_path_adds_roots_after_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIG_BOARD_PATH", os.pathsep.join(["", str(EXTRA), " "]))
    roots = boards.search_path()
    assert [(r.path, r.origin) for r in roots] == [
        (BOARDS, "jig.toml [paths] boards"),
        (EXTRA, "JIG_BOARD_PATH"),
    ]
    found = boards.catalog()["example-extra"]
    assert found.origin == "JIG_BOARD_PATH"
    assert boards.find("example-extra").root == FIXTURES


def test_a_board_defined_twice_names_both_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIG_BOARD_PATH", str(SHADOW))
    with pytest.raises(ConfigError) as error:
        boards.catalog()
    message = str(error.value)
    assert "board 'example-stm32' is defined twice" in message
    assert str(BOARDS / "example-stm32" / "board.yaml") in message
    assert str(SHADOW / "example-stm32" / "board.yaml") in message
    assert "(jig.toml [paths] boards)" in message and "(JIG_BOARD_PATH)" in message


def test_one_root_named_twice_is_not_a_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIG_BOARD_PATH", str(BOARDS))
    assert len(boards.search_path()) == 2
    assert "example-zynq" in boards.catalog()


def test_a_missing_root_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIG_BOARD_PATH", str(tmp_path / "absent"))
    with pytest.raises(ConfigError, match=r"JIG_BOARD_PATH: .*absent is not a directory"):
        boards.search_path()


class _EntryPoint:
    def __init__(self, name: str, value: str, loaded: Any) -> None:
        self.name = name
        self.value = value
        self._loaded = loaded

    def load(self) -> Any:
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


def test_the_jig_boards_entry_points_come_last(monkeypatch: pytest.MonkeyPatch) -> None:
    points = [
        _EntryPoint("extra", "pkg:root", lambda: EXTRA),
        _EntryPoint("more", "pkg:ROOTS", [str(EXTRA)]),
    ]
    monkeypatch.setattr(boards.metadata, "entry_points", lambda group: points)
    roots = boards.search_path()
    assert [r.origin for r in roots] == [
        "jig.toml [paths] boards",
        "entry point extra (pkg:root)",
        "entry point more (pkg:ROOTS)",
    ]
    assert boards.catalog()["example-extra"].origin == "entry point extra (pkg:root)"
    broken = [_EntryPoint("broken", "pkg:gone", ImportError("no module named pkg"))]
    monkeypatch.setattr(boards.metadata, "entry_points", lambda group: broken)
    with pytest.raises(ConfigError, match=r"entry point broken \(pkg:gone\): ImportError"):
        boards.search_path()
    wrong = [_EntryPoint("wrong", "pkg:NUMBER", 42)]
    monkeypatch.setattr(boards.metadata, "entry_points", lambda group: wrong)
    with pytest.raises(ConfigError, match="expected a directory or a list of them"):
        boards.search_path()


def test_without_paths_boards_the_project_boards_directory_is_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "jig.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    assert boards.search_path() == []
    board_file(tmp_path / "boards" / "x", MINIMAL, "x.repl")
    (root,) = boards.search_path()
    assert root.origin == "<project>/boards" and boards.available() == ["x"]


# ------------------------------------------------------------------ schema 2


def test_schema_2_resolution_of_a_plugin_board() -> None:
    board = boards.find("example-node")
    directory = BOARDS / "example-node"
    assert board.source == directory / "board.yaml"
    assert board.directory == directory
    assert board.root == FIXTURES  # root: ../..
    assert board.soc_platform == "xilinx/zynq7000"
    assert board.soc_driver is None
    assert board.platform == directory / "example-node.repl"  # YAML-relative
    assert board.firmware["app"] == FIXTURES / "build" / "node.elf"  # root-relative
    assert board.plugin == directory / "plugin.py"
    assert board.models == (directory / "models" / "ExampleBlock.cs",)
    assert board.power_initial == "off"
    assert board.interface("console").eol == "\r\n"
    assert board.interface("kiss").eol == "\r"
    assert board.soc_settings == {"straps": "jtag"}
    settings = board.section("csp")
    assert isinstance(settings, CspSettings)
    assert settings.address == 1
    assert settings.housekeeping is not None
    assert settings.housekeeping.port == 10
    assert settings.housekeeping.request == b"\x01"
    assert settings.housekeeping.codec == "example-status"
    assert settings.housekeeping.options == {"label": "status"}
    assert settings.rdp == {"ack_timeout_ms": 100}
    package = boards.find("example-package")
    assert package.plugin == BOARDS / "example-package" / "plugin"
    assert package.section("csp").housekeeping.request == b""
    assert boards.find("example-zynq").section("csp").housekeeping is None


def test_the_root_defaults_to_the_nearest_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    path = board_file(repo / "boards" / "x", MINIMAL + "firmware: {app: build/x.elf}\n", "x.repl")
    board = boards.load(path)
    assert board.root == repo.resolve()
    assert board.firmware["app"] == repo.resolve() / "build" / "x.elf"
    # A submodule or worktree checkout has a .git file.
    sub = repo / "external" / "sub"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/sub\n", encoding="utf-8")
    nested = board_file(sub / "boards" / "x", MINIMAL, "x.repl")
    assert boards.load(nested).root == sub.resolve()
    # Without one, the board's directory.
    loose = board_file(tmp_path / "loose" / "x", MINIMAL, "x.repl")
    assert boards.load(loose).root == loose.parent.resolve()


def test_an_explicit_root_wins(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "elsewhere").mkdir()
    path = board_file(tmp_path / "b", MINIMAL + "root: ../elsewhere\n", "x.repl")
    assert boards.load(path).root == (tmp_path / "elsewhere").resolve()
    path.write_text(MINIMAL + "root: ../missing\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"root .*missing is not a directory"):
        boards.load(path)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("schema: 1\n", "schema 1 is not supported"),
        ("dsl: dsl.py\n", "'dsl' is gone in schema 2; use plugin"),
        ("colour: blue\n", r"unknown key colour \(known: schema, name"),
        ("soc_driver: NoColon\n", "soc_driver must be 'module:Class'"),
        ("soc_platform: xilinx/zynq9000\n", "soc_platform: Jig ships no platform"),
        ("plugin: absent.py\n", "plugin .*absent.py not found"),
        ("power: {initial: dim}\n", "power.initial must be on or off"),
        ("power: {initial: off, fuse: 1}\n", "unknown key fuse"),
        ("models: [models/*.cs]\n", "models/\\*.cs' matches no file"),
        ("models: models/x.cs\n", "expected a list of glob patterns"),
        ("interfaces: {c: {kind: uart, peripheral: u, eol: '\\r'}}\n", None),
        ("interfaces: {c: {kind: uart, peripheral: u, eol: tab}}\n", "eol must be cr, lf or crlf"),
        ("interfaces: {c: {kind: can, peripheral: c, eol: cr}}\n", "unknown key eol"),
        ("interfaces: {c: {kind: uart, peripheral: u, baud: -1}}\n", "baud must be a positive"),
        ("storage: {f: {peripheral: f, size: 4, kind: nor}}\n", "unknown key kind"),
        ("csp: {address: 40}\n", "csp.address must be an integer 0-31"),
        ("csp: {address: 1, ping: true}\n", "csp: unknown key ping"),
        (
            "csp: {housekeeping: hk.yaml}\n",
            "a table {port, request, codec} since board.yaml schema 2",
        ),
        ("csp: {housekeeping: {port: 7}}\n", "missing 'codec'"),
        ("csp: {housekeeping: {port: 99, codec: c}}\n", "port must be a port number 0-63"),
        ("csp: {housekeeping: {port: 7, codec: c, request: 'zz'}}\n", "not hex digits"),
        ("csp: {housekeeping: {port: 7, codec: c, request: 300}}\n", "300 is not a byte"),
        ("csp: {rdp: {window: 4}}\n", "unknown option window"),
        ("csp: {rdp: {delayed_acks: 1}}\n", "delayed_acks must be true or false"),
        ("csp: {rdp: {ack_timeout_ms: 0}}\n", "ack_timeout_ms must be a positive integer"),
    ],
)
def test_board_yaml_errors(tmp_path: Path, extra: str, message: str | None) -> None:
    text = MINIMAL.replace("schema: 2\n", "") if extra.startswith("schema") else MINIMAL
    path = board_file(tmp_path / "b", text + extra, "x.repl", "models/readme.txt")
    if message is None:
        assert boards.load(path).interfaces[0].eol == "\r"  # '\r' in single quotes
        return
    with pytest.raises(ConfigError, match=message):
        boards.load(path)


def test_housekeeping_requests_in_every_form(tmp_path: Path) -> None:
    for written, wanted in (
        ('"01"', b"\x01"),
        ('"0x0102"', b"\x01\x02"),
        ('"01 02 ff"', b"\x01\x02\xff"),
        ("0x01", b"\x01"),
        ("[1, 2]", b"\x01\x02"),
        ("[]", b""),
    ):
        text = MINIMAL + f"csp: {{housekeeping: {{port: 7, codec: c, request: {written}}}}}\n"
        path = board_file(tmp_path / "b", text, "x.repl")
        assert boards.load(path).section("csp").housekeeping.request == wanted, written


def test_power_initial_reads_bare_on_and_off(tmp_path: Path) -> None:
    for written, wanted in (("on", "on"), ("off", "off"), ('"off"', "off"), ("true", "on")):
        path = board_file(tmp_path / "b", MINIMAL + f"power: {{initial: {written}}}\n", "x.repl")
        assert boards.load(path).power_initial == wanted


def test_models_globs_resolve_against_the_board_directory(tmp_path: Path) -> None:
    for name in ("models/a/One.cs", "models/b/Two.cs", "models/b/notes.txt", "shared/Three.cs"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    path = board_file(
        tmp_path / "boards" / "x",
        MINIMAL + 'models: ["../../models/**/*.cs", "../../shared/Three.cs"]\n',
        "x.repl",
    )
    board = boards.load(path)
    assert [p.relative_to(tmp_path.resolve()).as_posix() for p in board.models] == [
        "models/a/One.cs",
        "models/b/Two.cs",
        "shared/Three.cs",
    ]
    path.write_text(MINIMAL + 'models: ["../../models/b/*"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"matches notes.txt, not a .cs file"):
        boards.load(path)


def test_model_sets_hold_each_file_once_per_repository(tmp_path: Path) -> None:
    node = boards.find("example-node")
    package = boards.find("example-package")
    (only,) = boards.model_sets([node, package, node])
    assert only.root == FIXTURES
    assert only.name == "fixtures"
    assert only.files == node.models
    other_root = tmp_path / "other"
    (other_root / ".git").mkdir(parents=True)
    (other_root / "m").mkdir()
    (other_root / "m" / "Four.cs").write_text("", encoding="utf-8")
    other = boards.load(board_file(other_root / "b", MINIMAL + "models: [../m/*.cs]\n", "x.repl"))
    first, second = boards.model_sets([package, other])
    assert (first.root, second.root) == (FIXTURES, other_root.resolve())
    assert second.files == (other_root.resolve() / "m" / "Four.cs",)
    assert boards.model_sets([boards.find("example-zynq")]) == []


def test_library_directories_belong_to_boards() -> None:
    assert boards.library_dir("example-node") == BOARDS / "example-node" / "lib"
