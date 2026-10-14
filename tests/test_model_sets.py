"""C# model sets: membership, board sets, compiler messages and generated SDK projects."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from jig.errors import ConfigError, ToolchainError
from jig.renode import bridge, models
from jig.renode.models import ModelCompileError, ModelSet


def _cs(path: Path, text: str = "namespace Antmicro.Renode.Peripherals.Test { }\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_jig_ships_its_models_and_the_bridge() -> None:
    jig = models.jig_model_set()
    names = {path.relative_to(jig.root).as_posix() for path in jig.files}
    assert {"Harness/Bridge.cs", "Harness/Stepping.cs", "Contracts/ISpiNorFlash.cs"} <= names
    assert all(path.suffix == ".cs" for path in jig.paths())


def test_the_bridge_protocol_matches_its_csharp_side() -> None:
    source = (models.jig_models_dir() / "Harness" / "Bridge.cs").read_text(encoding="utf-8")
    match = re.search(r"public const int ProtocolVersion = (\d+);", source)
    assert match is not None and int(match.group(1)) == bridge.PROTOCOL_VERSION


def test_a_set_checks_its_files(tmp_path: Path) -> None:
    root = tmp_path / "models"
    a = _cs(root / "A.cs")
    model_set = ModelSet("board", root, (Path("A.cs"), a))
    assert model_set.paths() == (a.resolve(),)
    assert model_set.display(a) == "A.cs"
    with pytest.raises(ConfigError, match="does not exist"):
        ModelSet("board", root, (Path("B.cs"),)).paths()
    with pytest.raises(ConfigError, match=r"is not a \.cs file"):
        ModelSet("board", root, (_cs(root / "notes.txt"),)).paths()
    with pytest.raises(ConfigError, match=r"has no \.cs files"):
        ModelSet("board", root, ()).paths()


def test_globs_and_board_directories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _cs(repo / "models" / "Vendor" / "Part.cs")
    _cs(repo / "models" / "Other.cs")
    board = repo / "boards" / "example"
    board.mkdir(parents=True)
    (board / "board.yaml").write_text(
        "name: example\nroot: ../..\nmodels: ['models/**/*.cs']\n", encoding="utf-8"
    )
    model_set = models.board_model_set(board)
    assert model_set.name == "example"
    assert model_set.root == repo.resolve()
    assert sorted(model_set.display(p) for p in model_set.files) == [
        "models/Other.cs",
        "models/Vendor/Part.cs",
    ]
    plain = tmp_path / "plain"
    _cs(plain / "models" / "X.cs")
    assert [p.name for p in models.board_model_set(plain).files] == ["X.cs"]
    with pytest.raises(ConfigError, match=r"matches no \.cs file"):
        ModelSet.glob("none", tmp_path, ["nothing/*.cs"])
    with pytest.raises(ConfigError, match="no C# models"):
        models.board_model_set(_cs(tmp_path / "empty" / "x.txt").parent)


def test_compiler_messages_map_to_their_set_and_file(tmp_path: Path) -> None:
    root = tmp_path / "board"
    broken = _cs(root / "Vendor" / "Broken.cs")
    other = tmp_path / "elsewhere.cs"
    model_set = ModelSet("board", root, (broken,))
    runtime = (
        "Errors during compilation or loading:\r\n"
        f"Could not compile assembly from: {broken}\r\n"
        f"{broken}(7,13): error CS1002: ; expected\r\n"
        f"{other}(1,1): error CS0246: The type or namespace name 'Foo' could not be found\r\n"
    )
    found = models.parse_diagnostics(runtime, [model_set], default="board")
    assert [str(d) for d in found] == [
        "board: Vendor/Broken.cs(7,13): error CS1002: ; expected",
        f"board: {other}(1,1): error CS0246: The type or namespace name 'Foo' could not be found",
    ]
    csproj = tmp_path / "Check.csproj"
    sdk = (
        f"{broken}(7,13): error CS1002: ; expected [{csproj}]\n"
        f"{broken}(9,1): warning CS0168: The variable 'x' is declared [{csproj}]\n"
        f"{broken}(7,13): error CS1002: ; expected [{csproj}]\n"
    )
    found = models.parse_diagnostics(sdk, [model_set])
    assert [(d.file, d.line, d.severity, d.code) for d in found] == [
        ("Vendor/Broken.cs", 7, "error", "CS1002"),
        ("Vendor/Broken.cs", 9, "warning", "CS0168"),
    ]
    error = ModelCompileError("board", found, sdk)
    assert "model set board does not compile" in str(error)
    assert "board: Vendor/Broken.cs(7,13): error CS1002: ; expected" in str(error)
    assert "CS0168" not in str(error)  # warnings show only without errors


def test_an_sdk_project_names_every_file_and_renode_assembly(tmp_path: Path) -> None:
    renode = tmp_path / "renode"
    renode.mkdir()
    with pytest.raises(ToolchainError, match="not a Renode install"):
        models.write_sdk_project(tmp_path / "x.csproj", [], renode)
    for name in models.SDK_REFERENCES:
        (renode / f"{name}.dll").write_bytes(b"")
    board = ModelSet("board", tmp_path / "b", (_cs(tmp_path / "b" / "Part.cs"),))
    project = models.write_sdk_project(
        tmp_path / "out" / "Check.csproj",
        [models.jig_model_set(), board],
        renode,
        output_dir=tmp_path / "build",
    )
    tree = ET.parse(project)
    compiled = [e.get("Include") for e in tree.iter("Compile")]
    links = [e.get("Link") for e in tree.iter("Compile")]
    assert len(compiled) == len(models.jig_model_set().files) + 1
    assert "board/Part.cs" in links and "jig/Harness/Bridge.cs" in links
    hints = {e.get("Include"): e.findtext("HintPath") for e in tree.iter("Reference")}
    assert hints["Infrastructure"] == (renode / "Infrastructure.dll").as_posix()
    assert set(hints) == set(models.SDK_REFERENCES)
    assert tree.findtext("PropertyGroup/TreatWarningsAsErrors") == "true"
    assert (
        tree.findtext("PropertyGroup/BaseOutputPath")
        == f"{(tmp_path / 'build' / 'bin').as_posix()}/"
    )
    # The output paths come before the SDK's props read them (else MSB3539).
    first, second, *_rest, last = list(tree.getroot())
    obj = f"{(tmp_path / 'build' / 'obj').as_posix()}/"
    assert first.findtext("BaseIntermediateOutputPath") == obj
    assert (second.tag, second.get("Project"), second.get("Sdk")) == (
        "Import",
        "Sdk.props",
        "Microsoft.NET.Sdk",
    )
    assert (last.tag, last.get("Project")) == ("Import", "Sdk.targets")


def test_boards_are_named_by_directory(tmp_path: Path) -> None:
    first = tmp_path / "first"
    _cs(first / "models" / "A.cs")
    second = tmp_path / "second"
    _cs(second / "B.cs")
    sets = models.board_sets([first, str(second)])
    assert [s.name for s in sets] == ["first", "second"]
    twin = tmp_path / "twin" / "first"
    _cs(twin / "C.cs")
    with pytest.raises(ConfigError, match="names must differ"):
        models.board_sets([first, twin])
