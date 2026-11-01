"""The plugin API: loading board plugins (a file or a package) in isolation,
what ``register(api)`` may register, and how registrations are found."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import jig.plugin
from jig.errors import ConfigError
from jig.ext.zynq7000.driver import ZynqHooks
from jig.plugin import API_VERSION, PluginApi
from jig.system import boards, registry
from jig.system.registry import Member, Owner, Registry
from jig.system.socs import driver_for
from jig.system.socs.stm32 import Stm32Driver

from .conftest import FIXTURES


def plugin_board(directory: Path, plugin: str, *, files: dict[str, str], device: str = "") -> Path:
    """A board.jig naming ``plugin``, with the plugin's files beside it."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    device = device or 'device mcu: stm32(part: "STM32F407VG") { firmware: "x.elf" }'
    path = directory / "board.jig"
    path.write_text(
        f'board "{directory.name}" {{\n    plugin: "{plugin}"\n    {device}\n}}\n',
        encoding="utf-8",
        newline="\n",
    )
    return path


# ------------------------------------------------------------------- loading


def test_a_file_plugin_and_a_package_plugin_load_side_by_side(
    _fresh_registry: Registry,
) -> None:
    path_before = list(sys.path)
    node = boards.find("example-node")
    package = boards.find("example-package")
    _fresh_registry.load_board(node)
    _fresh_registry.load_board(package)
    assert sys.path == path_before  # nothing joined the import path
    functions = _fresh_registry.board_functions(package)
    assert functions["package_name"].value() == "example-package"
    assert _fresh_registry.board_functions(node)["status_size"].value() == 36
    # Both plugins hold a module called helpers; each sees its own.
    node_module = registry.module_name(node.plugin)  # type: ignore[arg-type]
    package_module = registry.module_name(package.plugin)  # type: ignore[arg-type]
    assert node_module != package_module
    assert sys.modules[f"{node_module}.helpers"].NAME == "example-node"
    assert sys.modules[f"{package_module}.helpers"].NAME == "example-package"
    # The package's subpackage imported its sibling relatively.
    assert f"{package_module}.services.uptime" in sys.modules
    assert "helpers" not in sys.modules


def test_a_plugin_runs_once_per_board_definition(_fresh_registry: Registry) -> None:
    node = boards.find("example-node")
    first = _fresh_registry.load_board(node)
    again = _fresh_registry.load_board(boards.find("example-node"))
    assert first is again == _fresh_registry.boards[node.source]
    assert first == Owner("board", "example-node", node.source)
    assert len(_fresh_registry.member_entries("board", "origin")) == 1


def test_a_board_without_a_plugin_registers_nothing(_fresh_registry: Registry) -> None:
    board = boards.find("example-zynq")
    owner = _fresh_registry.load_board(board)
    assert owner.name == "example-zynq"
    assert _fresh_registry.board_functions(board) == {}


def test_two_boards_may_share_one_plugin_file(tmp_path: Path, _fresh_registry: Registry) -> None:
    shared = tmp_path / "plugins" / "shared.py"
    shared.parent.mkdir()
    shared.write_text(
        "def register(api):\n"
        "    api.hk_codec('shared-codec', lambda context: lambda payload: {})\n"
        "    api.member('board', 'kind', lambda board: board.board)\n"
        "    api.node_kind('radio', lambda: None)\n"
        "    api.bus_observer('frames', lambda context: [])\n",
        encoding="utf-8",
    )
    first = boards.load(
        plugin_board(tmp_path / "boards" / "one", "../../plugins/shared.py", files={})
    )
    second = boards.load(
        plugin_board(tmp_path / "boards" / "two", "../../plugins/shared.py", files={})
    )
    _fresh_registry.load_board(first)
    _fresh_registry.load_board(second)
    assert len(_fresh_registry.member_entries("board", "kind")) == 2
    assert _fresh_registry.codec("shared-codec").owner.name == "one"
    # A system holding only the second board still finds what the file added.
    assert _fresh_registry.node_kinds_for([second.source])["radio"].owner.name == "two"
    assert _fresh_registry.bus_observers_for([second.source])["frames"].owner.name == "two"
    assert "radio" not in _fresh_registry.node_kinds_for([])


@pytest.mark.parametrize(
    ("files", "plugin", "message"),
    [
        ({"p.py": "x = 1\n"}, "p.py", r"plugin .*p.py defines no register\(api\)"),
        ({"p.py": "raise RuntimeError('broken')\n"}, "p.py", "importing plugin .*: RuntimeError"),
        (
            {"p.py": "def register(api):\n    raise KeyError('k')\n"},
            "p.py",
            r"register\(api\) failed: KeyError",
        ),
        ({"p.py": "JIG_PLUGIN_API = 1\ndef register(api): pass\n"}, "p.py", "plugin API 1"),
        ({"pkg/mod.py": ""}, "pkg", "plugin package .*pkg has no __init__.py"),
        ({"p.txt": ""}, "p.txt", "is not a .py file or a package"),
        (
            {"p.py": "def register(api):\n    api.function('f', 1)\n"},
            "p.py",
            "function f must be callable",
        ),
    ],
)
def test_plugin_errors_name_the_board(
    tmp_path: Path, _fresh_registry: Registry, files: dict[str, str], plugin: str, message: str
) -> None:
    board = boards.load(plugin_board(tmp_path / "broken", plugin, files=files))
    with pytest.raises(ConfigError, match=message) as error:
        _fresh_registry.load_board(board)
    assert "broken" in str(error.value)
    # Nothing it staged before failing is kept.
    assert board.source not in _fresh_registry.boards


def test_the_api_version_is_two() -> None:
    assert API_VERSION == 2 == PluginApi.version
    assert jig.plugin.__doc__ is not None and "Nothing outside this module is public" in (
        jig.plugin.__doc__
    )
    for name in jig.plugin.__all__:
        assert hasattr(jig.plugin, name), name


# ---------------------------------------------------------------- the API


def api_for(tmp_path: Path) -> PluginApi:
    board = boards.load(plugin_board(tmp_path / "b", "p.py", files={"p.py": ""}))
    return PluginApi(Owner("board", board.name, board.source), board)


def test_the_api_knows_its_board(tmp_path: Path) -> None:
    api = api_for(tmp_path)
    assert api.name == "b"
    assert api.directory == (tmp_path / "b").resolve()
    assert api.root == api.board.root
    with pytest.raises(ConfigError, match="serves no single board"):
        PluginApi(Owner("extension", "x")).board  # noqa: B018


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda api: api.function("not a name", print), "is not a name a plan can use"),
        (lambda api: api.function("check", print), "is not a name a plan can use"),
        (lambda api: api.function("_hidden", print), "is not a name a plan can use"),
        (lambda api: api.member("radio", "x", print), "handle type 'radio' is not one of"),
        (lambda api: api.member("board", "console", print), "board member 'console' is built in"),
        (lambda api: api.member("node", "ping", print), "node member 'ping' is built in"),
        (lambda api: api.member("bus", "frames", print), "bus member 'frames' is built in"),
        (lambda api: api.hk_codec("Bad Name", print), "codec name 'Bad Name'"),
        (lambda api: api.device_family("Z80!", print), "device family 'Z80!'"),
        (lambda api: api.zynq_hooks(), "needs at least one of images"),
        (lambda api: api.zynq_hooks(images=3), "zynq_hooks images must be callable"),
        (lambda api: api.background("node", "beat", print, every=0), "positive number"),
    ],
)
def test_the_api_checks_what_it_is_given(tmp_path: Path, call: Any, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        call(api_for(tmp_path))


def test_registering_one_name_twice_in_a_plugin_is_an_error(tmp_path: Path) -> None:
    api = api_for(tmp_path)
    api.function("f", print)
    api.function("f", print)  # the same object again is fine
    with pytest.raises(ConfigError, match="registers function 'f' twice"):
        api.function("f", repr)


def test_registrations_are_staged_until_register_returns(tmp_path: Path) -> None:
    api = api_for(tmp_path)
    api.member("board", "flag", print, attribute=True)
    api.zynq_hooks(images=print)
    assert api.staged.members[("board", "flag")] == Member(print, attribute=True)
    assert api.staged.hooks["zynq7000"] == ZynqHooks(images=print)


def test_codecs_node_kinds_and_observers_clash_across_plugins(tmp_path: Path) -> None:
    fresh = Registry()
    first, second = Owner("board", "a", tmp_path / "a"), Owner("board", "b", tmp_path / "b")
    api = PluginApi(first)
    api.hk_codec("status", print)
    fresh.commit(api.staged)
    other = PluginApi(second)
    other.hk_codec("status", repr)
    with pytest.raises(ConfigError, match="registers housekeeping codec 'status', which board a"):
        fresh.commit(other.staged)
    # A board member is scoped to its board: the same name elsewhere is fine.
    for owner in (first, second):
        scoped = PluginApi(owner)
        scoped.member("board", "origin", lambda board, owner=owner: owner.name)
        fresh.commit(scoped.staged)
    assert len(fresh.member_entries("board", "origin")) == 2


def test_extension_members_are_global(_fresh_registry: Registry) -> None:
    _fresh_registry.ensure_extensions()
    (address,) = _fresh_registry.member_entries("board", "address")
    assert address.owner == Owner("extension", "jig.ext.csp")
    assert address.value.attribute
    assert {"packets", "traffic", "hk"} <= set(_fresh_registry.member_names("bus", []))
    assert "csp" in _fresh_registry.node_kinds_for([])
    assert "crc32c" in _fresh_registry.extension_functions()
    clash = PluginApi(Owner("board", "x", Path("x")))
    clash.member("bus", "packets", print)
    with pytest.raises(ConfigError, match="registers bus member 'packets', which extension"):
        _fresh_registry.commit(clash.staged)


# ------------------------------------------------------------ device families


def test_driver_for_follows_the_documented_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fresh_registry: Registry
) -> None:
    driver_source = (
        "class CustomDriver:\n"
        "    name = 'custom'\n"
        "    boot_modes = ('flash',)\n"
        "    def arguments(self, values, where):\n"
        "        if 'bad' in values:\n"
        "            raise ValueError('no')\n"
        "        return dict(values)\n"
        "    def overlay(self, config): return ''\n"
        "    def configure(self, config, session): pass\n"
        "    def boot(self, config, session): pass\n"
    )
    directory = tmp_path / "custom"
    # 1. the device's driver setting: file.py:Class beside the board file
    path = plugin_board(
        directory,
        "p.py",
        files={"p.py": "def register(api): pass\n", "drivers.py": driver_source},
        device='device d: custom(speed: 3) { driver: "drivers.py:CustomDriver" }',
    )
    board = boards.load(path)
    assert board.soc == "custom" and board.soc_settings == {"speed": 3}
    assert type(driver_for(board)).__name__ == "CustomDriver"
    # ... or module:Class
    path.write_text(
        path.read_text().replace("drivers.py:CustomDriver", "jig.system.socs.stm32:Stm32Driver"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"stm32\(\) takes part"):  # Stm32Driver's arguments
        driver_for(boards.load(path))
    # 2. the family the board's plugin registered
    path.write_text(
        path.read_text().replace(' { driver: "jig.system.socs.stm32:Stm32Driver" }', " { }"),
        encoding="utf-8",
    )
    (directory / "p.py").write_text(
        "from .drivers import CustomDriver\n"
        "def register(api):\n    api.device_family('custom', CustomDriver)\n",
        encoding="utf-8",
    )
    board = boards.load(path)  # a family Jig does not know waits for the plugin
    with pytest.raises(ConfigError, match="no device family 'custom'"):
        driver_for(board)
    _fresh_registry.load_board(board)
    assert type(driver_for(board)).__name__ == "CustomDriver"
    # Without a plugin or a driver setting, an unknown family is an error at once.
    alone = plugin_board(
        tmp_path / "alone", "p.py", files={"p.py": ""}, device="device d: custom() { }"
    )
    alone.write_text(alone.read_text().replace('    plugin: "p.py"\n', ""), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"no device family 'custom' \(known: stm32, zynq7000\)"):
        boards.load(alone)


def test_the_device_families_come_from_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import metadata

    from jig.system import socs

    installed = {
        entry.name: entry.value for entry in metadata.entry_points(group="jig.device_families")
    }
    assert installed == {
        "zynq7000": "jig.ext.zynq7000.driver:Zynq7000Driver",
        "stm32": "jig.system.socs.stm32:Stm32Driver",
    }
    point = metadata.EntryPoint(name="stm32", value="jig.system.socs.stm32:Stm32Driver", group="")
    twin = metadata.EntryPoint(name="stm32", value="other.module:Driver", group="")
    monkeypatch.setattr(socs, "_entry_points", lambda: {"stm32": [point, twin]})
    with pytest.raises(ConfigError, match="'stm32' is registered twice"):
        driver_for("stm32")
    monkeypatch.setattr(socs, "_entry_points", lambda: {})
    # A source tree that was never installed still knows the two built in.
    assert isinstance(driver_for("stm32"), Stm32Driver)
    assert socs.families() == ["stm32", "zynq7000"]
    with pytest.raises(ConfigError, match="no device family 'z80'"):
        driver_for("z80")


def test_zynq_hooks_reach_the_machine_config(tmp_path: Path, _fresh_registry: Registry) -> None:
    node = boards.find("example-node")
    _fresh_registry.load_board(node)
    hooks = _fresh_registry.hooks_for(node)["zynq7000"]
    assert isinstance(hooks, ZynqHooks) and hooks.images is not None
    assert hooks.qspi_image is None and hooks.boot_table is None


def test_fixture_plugins_are_public_api_only() -> None:
    """The fixture plugins import nothing from jig but jig.plugin."""
    for path in (FIXTURES / "boards").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        imports = [
            line for line in text.splitlines() if line.startswith(("from jig", "import jig"))
        ]
        assert all(line.startswith("from jig.plugin import") for line in imports), path
