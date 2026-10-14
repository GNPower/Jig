"""Projects: jig.toml discovery and settings, jig.local.toml, the per-user
cache, host-independent paths, and the files that ship in the package."""

from __future__ import annotations

from pathlib import Path

import platformdirs
import pytest

from jig import project
from jig.errors import ConfigError

from .conftest import FIXTURES


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_the_environment_names_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIG_PROJECT", str(FIXTURES))
    current = project.current()
    assert current.root == FIXTURES
    assert current.file == FIXTURES / "jig.toml"
    assert current.name == "fixtures"
    assert current.board_roots() == [FIXTURES / "boards"]
    assert current.lib == (FIXTURES / "lib",)


def test_jig_project_may_name_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = write(tmp_path / "elsewhere" / "custom.toml", '[project]\nname = "named"\n')
    monkeypatch.setenv("JIG_PROJECT", str(file))
    assert project.current().name == "named"
    assert project.root() == file.parent.resolve()


def test_a_missing_project_directory_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path / "absent"))
    with pytest.raises(ConfigError, match="is not a directory"):
        project.root()


def test_the_nearest_jig_toml_marks_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JIG_PROJECT")
    outer = tmp_path / "outer"
    inner = outer / "repo"
    work = inner / "plans" / "nested"
    work.mkdir(parents=True)
    write(outer / "jig.toml", "")
    write(inner / "jig.toml", "")
    monkeypatch.chdir(work)
    assert project.root() == inner.resolve()


def test_the_plan_file_decides_before_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JIG_PROJECT")
    here = tmp_path / "here"
    there = tmp_path / "there"
    write(here / "jig.toml", '[project]\nname = "here"\n')
    plan = write(there / "plans" / "x.jig", "")
    write(there / "jig.toml", '[project]\nname = "there"\n')
    monkeypatch.chdir(here)
    assert project.discover(plan).name == "there"
    assert project.discover().name == "here"
    outside = write(tmp_path / "loose" / "y.jig", "")
    assert project.discover(outside).name == "here"


def test_without_jig_toml_the_working_directory_is_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JIG_PROJECT")
    monkeypatch.chdir(tmp_path)
    if any((directory / "jig.toml").is_file() for directory in tmp_path.resolve().parents):
        pytest.skip("a jig.toml above the temporary directory")
    current = project.current()
    assert current.root == tmp_path.resolve()
    assert current.file is None
    assert current.board_roots() == []


def test_every_table_of_jig_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for directory in ("boards", "more/boards", "lib", "tools/renode"):
        (tmp_path / directory).mkdir(parents=True)
    write(
        tmp_path / "jig.toml",
        '[project]\nname = "demo"\n'
        '[paths]\nboards = ["boards", "more/boards"]\nlib = ["lib"]\n'
        '[renode]\npath = "tools/renode"\nversion = ">=1.17.0,<1.18"\n'
        '[defaults]\nboard = "example-zynq"\n'
        'options = { pl = "rtl", rate = 2, trace = true }\n',
    )
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    current = project.current()
    root = tmp_path.resolve()
    assert current.name == "demo"
    assert current.boards == (root / "boards", root / "more" / "boards")
    assert current.lib == (root / "lib",)
    assert current.renode_path == root / "tools" / "renode"
    assert current.renode_version == ">=1.17.0,<1.18"
    assert current.default_board == "example-zynq"
    assert current.default_options == {"pl": "rtl", "rate": "2", "trace": "true"}
    assert current.local is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[projekt]\n", r"unknown table \[projekt\] \(known: project, paths, renode, defaults\)"),
        ('[paths]\nboard = ["b"]\n', r"paths.board is not a setting \(known: boards, lib\)"),
        ('[paths]\nboards = "boards"\n', "paths.boards must be a list of paths"),
        ("[project]\nname = 3\n", "project.name must be a non-empty string"),
        ('[renode]\nversion = "1.17"\n', "renode.version must be a version specifier"),
        ("[defaults]\noptions = { pl = [1] }\n", "defaults.options.pl must be a string"),
        ("paths = 1\n", "paths must be a table"),
        ("[project\n", "jig.toml"),
        ('[paths]\nlib = ["missing"]\n', r"paths.lib: .*missing is not a directory"),
    ],
)
def test_jig_toml_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, message: str
) -> None:
    write(tmp_path / "jig.toml", text)
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    with pytest.raises(ConfigError, match=message):
        project.current()


def test_jig_local_toml_merges_on_top(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for directory in ("boards", "checkout/boards"):
        (tmp_path / directory).mkdir(parents=True)
    write(
        tmp_path / "jig.toml",
        '[paths]\nboards = ["boards"]\n[defaults]\nboard = "a"\noptions = { pl = "model" }\n',
    )
    write(
        tmp_path / "jig.local.toml",
        '[paths]\nboards = ["checkout/boards"]\n[defaults]\noptions = { trace = "on" }\n',
    )
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    current = project.current()
    assert current.local == tmp_path.resolve() / "jig.local.toml"
    # A list replaces the one in jig.toml; tables merge key by key.
    assert current.boards == (tmp_path.resolve() / "checkout" / "boards",)
    assert current.default_board == "a"
    assert current.default_options == {"pl": "model", "trace": "on"}
    write(tmp_path / "jig.local.toml", "[paths]\nmodels = []\n")
    with pytest.raises(ConfigError, match=r"jig.local.toml: paths.models is not a setting"):
        project.current()


def test_activate_overrides_discovery(tmp_path: Path) -> None:
    other = project.Project(root=tmp_path, name="other")
    assert project.current().name == "fixtures"
    with project.activate(other):
        assert project.current() is other
        assert project.root() == tmp_path
    assert project.current().name == "fixtures"


def test_output_defaults_to_the_project_and_follows_jig_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JIG_OUTPUT")
    assert project.output_dir() == FIXTURES / "out"
    monkeypatch.setenv("JIG_OUTPUT", str(tmp_path / "runs"))
    assert project.output_dir() == (tmp_path / "runs").resolve()


def test_the_cache_is_per_user_unless_jig_cache_dir_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JIG_CACHE_DIR")
    assert project.cache_dir() == Path(platformdirs.user_cache_dir("jig", appauthor=False))
    monkeypatch.setenv("JIG_CACHE_DIR", str(tmp_path / "cache"))
    cache = (tmp_path / "cache").resolve()
    assert project.cache_dir() == cache
    assert project.downloads_dir() == cache / "downloads"
    assert project.tools_dir() == cache / "tools"
    assert project.artifacts_dir() == cache / "artifacts"


def test_paths_in_text_become_host_independent() -> None:
    anchors = {
        "C:\\work\\repo": "",
        "C:\\work\\repo\\out": "<out>",
        "/home/ci/.cache/jig": "<cache>",
    }
    text = (
        "plan file C:\\work\\repo\\plans\\x.jig not found; "
        "image 'C:/work/repo/out/runs/1/flash.img', "
        "C:\\\\work\\\\repo\\\\lib\\\\a.jig, "
        "cache /home/ci/.cache/jig/artifacts/m-1/map.json; "
        "C:\\work\\repository\\x and C:\\work\\repo"
    )
    assert project.relativize(text, anchors) == (
        "plan file plans/x.jig not found; "
        "image '<out>/runs/1/flash.img', "
        "lib/a.jig, "
        "cache <cache>/artifacts/m-1/map.json; "
        "C:\\work\\repository\\x and ."
    )


def test_the_current_project_anchors_its_directories(tmp_path: Path) -> None:
    anchors = project.path_anchors()
    assert anchors[str(FIXTURES)] == ""
    assert anchors[str(project.cache_dir())] == "<cache>"
    assert "<out>" in anchors.values()


def test_package_files_resolve_through_the_package() -> None:
    grammar = project.package_file("lang", "grammar", "jig.tx")
    assert grammar.is_file()
    assert "textX grammar" in grammar.read_text(encoding="utf-8")
    models = project.package_file("models")
    assert {path.parent.name for path in models.rglob("*.cs")} == {
        "Arm",
        "Cadence",
        "Contracts",
        "Harness",
        "ST",
        "Xilinx",
    }
    assert project.package_file("py.typed").is_file()
    stdlib = project.package_file("lang", "stdlib")
    assert sorted(p.name for p in stdlib.glob("*.jig")) == [
        "console.jig",
        "records.jig",
        "text.jig",
    ]


def test_platforms_are_found_by_name() -> None:
    from jig.renode import platforms

    platform = project.platform_file("xilinx/zynq7000")
    assert platform.name == "zynq7000.repl"
    assert "Jig.Xilinx.Zynq7000_SLCR" in platform.read_text(encoding="utf-8")
    assert "xilinx/zynq7000" in platforms.available()
    with pytest.raises(ConfigError, match="ships no platform 'xilinx/zynq9000'"):
        project.platform_file("xilinx/zynq9000")
    for bad in ("../secrets", "Xilinx/Zynq7000", "xilinx/../x", ""):
        with pytest.raises(ConfigError, match="is not a platform name"):
            platforms.path(bad)
