"""The Renode-side commands of ``jig``.

``add_commands(subparsers)`` adds ``doctor``, ``renode`` (``install``,
``list``, ``path``, ``remove``), ``models`` (``check``, ``ide``) and ``cosim``
(``build``) to the ``jig`` parser; ``run(args)`` runs the one chosen. Each
sets ``func=run``, so a parser that dispatches through ``args.func(args)``
needs nothing else. ``python -m jig.renode`` runs them without the rest of
the command line.

Exit codes: 0 success, 1 a check found problems (``doctor``, ``models
check``), 2 a usage or configuration error (including a download nobody
agreed to), 3 an infrastructure failure (Renode missing or not starting, a
download or checksum failure, a failed build).

``add_renode_options(parser)`` adds ``--renode`` and
``--allow-untested-renode``; ``resolve_install(args)`` turns them, the
project's ``jig.toml`` and the environment into a ``RenodeInstall``,
offering ``jig renode install`` at a terminal when nothing is found.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from jig import project
from jig.errors import ConfigError, JigError
from jig.renode import assets, discover, doctor, install, models
from jig.renode.discover import RenodeInstall

log = logging.getLogger(__name__)

EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, EXIT_INFRASTRUCTURE = 0, 1, 2, 3

_KEY = "jig_renode_command"


# ------------------------------------------------------------------ parsing


def add_renode_options(parser: argparse.ArgumentParser) -> None:
    """``--renode PATH`` and ``--allow-untested-renode``."""
    parser.add_argument(
        "--renode",
        type=Path,
        metavar="PATH",
        help="the Renode to use: its executable or directory (overrides every other source)",
    )
    parser.add_argument(
        "--allow-untested-renode",
        action="store_true",
        help=f"accept a Renode outside {discover.SUPPORTED}",
    )


def add_commands(subparsers: Any) -> None:
    """Add Jig's Renode commands to ``subparsers`` (``ArgumentParser.add_subparsers()``)."""
    doctor_parser = subparsers.add_parser(
        "doctor", help="check Python, Renode and the models end to end"
    )
    doctor_parser.add_argument("--json", action="store_true", help="print the report as JSON")
    doctor_parser.add_argument(
        "--board",
        action="append",
        default=[],
        metavar="BOARD",
        help="also compile this board's C# models: a directory or a name (repeatable)",
    )
    add_renode_options(doctor_parser)
    doctor_parser.set_defaults(func=run, **{_KEY: _doctor})

    renode = subparsers.add_parser("renode", help="find, install and remove Renode")
    renode_commands = renode.add_subparsers(dest="renode_command", metavar="COMMAND")
    renode_commands.required = True
    choices = sorted({a.platform for a in assets.ASSETS})

    install_parser = renode_commands.add_parser(
        "install", help="download, verify and install a Renode release into Jig's cache"
    )
    install_parser.add_argument(
        "version", nargs="?", default=None, help=f"the version (default {assets.DEFAULT_VERSION})"
    )
    install_parser.add_argument(
        "--platform",
        choices=choices,
        help="install the build for this platform (default: this host)",
    )
    install_parser.add_argument("--yes", "-y", action="store_true", help="agree to the download")
    install_parser.add_argument("--force", action="store_true", help="reinstall even if present")
    install_parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        metavar="FILE",
        help="install from this archive instead of downloading it (checked all the same)",
    )
    install_parser.set_defaults(func=run, **{_KEY: _renode_install})

    list_parser = renode_commands.add_parser("list", help="list Jig-managed Renode installs")
    list_parser.add_argument("--json", action="store_true", help="print JSON")
    list_parser.set_defaults(func=run, **{_KEY: _renode_list})

    path_parser = renode_commands.add_parser("path", help="print the Renode that Jig would run")
    add_renode_options(path_parser)
    path_parser.set_defaults(func=run, **{_KEY: _renode_path})

    remove_parser = renode_commands.add_parser("remove", help="remove a Jig-managed Renode install")
    remove_parser.add_argument("version", help="the version to remove")
    remove_parser.add_argument(
        "--platform", choices=choices, help="its platform (default: this host)"
    )
    remove_parser.set_defaults(func=run, **{_KEY: _renode_remove})

    models_parser = subparsers.add_parser("models", help="check C# model sets")
    model_commands = models_parser.add_subparsers(dest="models_command", metavar="COMMAND")
    model_commands.required = True

    check = model_commands.add_parser(
        "check", help="compile model sets in a throwaway Renode (or with the .NET SDK)"
    )
    check.add_argument(
        "--board",
        action="append",
        default=[],
        metavar="BOARD",
        help="a board directory or name whose models compile after Jig's (repeatable)",
    )
    check.add_argument(
        "--sdk",
        action="store_true",
        help="build with the .NET SDK instead, warnings as errors (needs dotnet)",
    )
    add_renode_options(check)
    check.set_defaults(func=run, **{_KEY: _models_check})

    ide = model_commands.add_parser(
        "ide", help="write a csproj for an editor: a board's models, Jig's, and Renode's assemblies"
    )
    ide.add_argument(
        "--board",
        metavar="BOARD",
        help="a board directory or name (default: Jig's models only)",
    )
    ide.add_argument(
        "--output",
        "-o",
        type=Path,
        metavar="FILE",
        help="the csproj to write (default: <model root>/<name>.Models.csproj)",
    )
    add_renode_options(ide)
    ide.set_defaults(func=run, **{_KEY: _models_ide})

    cosim = subparsers.add_parser("cosim", help="RTL co-simulation")
    cosim_commands = cosim.add_subparsers(dest="cosim_command", metavar="COMMAND")
    cosim_commands.required = True
    build = cosim_commands.add_parser(
        "build", help="build a Verilated AXI4-Lite design into a Renode co-simulation library"
    )
    add_cosim_build_arguments(build)
    build.set_defaults(func=run, **{_KEY: _cosim_build})


def add_cosim_build_arguments(build: argparse.ArgumentParser) -> None:
    build.add_argument("output", type=Path, help="the build directory (the library is lib<TOP>.so)")
    build.add_argument("--top", required=True, help="the top module")
    build.add_argument(
        "--sources", required=True, type=Path, metavar="FILE", help="the Verilator file list"
    )
    build.add_argument(
        "--root",
        type=Path,
        metavar="DIR",
        help="where the list's paths start (default: its directory)",
    )
    build.add_argument(
        "--clock-mhz", type=int, metavar="N", help="the model clock, as -G<clock-param>=N"
    )
    build.add_argument(
        "--clock-param",
        default="CLK_MHZ",
        metavar="NAME",
        help="the top's clock parameter (default CLK_MHZ)",
    )
    build.add_argument(
        "-G",
        dest="parameters",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a top parameter (repeatable)",
    )
    build.add_argument(
        "--irq",
        dest="interrupts",
        action="append",
        default=[],
        metavar="SIGNAL",
        help="an interrupt output, GPIO 0 first (repeatable)",
    )
    build.add_argument("--prefix", default="", help="prefix of the top's AXI4-Lite signal names")
    build.add_argument("--clock", default="clk", help="the clock input (default clk)")
    build.add_argument(
        "--reset", default="rst_n", help="the active-low reset input (default rst_n)"
    )
    build.add_argument(
        "--cxx",
        action="append",
        default=[],
        type=Path,
        metavar="FILE",
        help="an extra C++ source (repeatable)",
    )
    build.add_argument(
        "-I",
        dest="include_dirs",
        action="append",
        default=[],
        type=Path,
        metavar="DIR",
        help="a C++ include directory (repeatable)",
    )
    build.add_argument(
        "--verilator-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="an extra Verilator argument (repeatable)",
    )
    build.add_argument(
        "--integration-library", type=Path, metavar="DIR", help="Renode's IntegrationLibrary"
    )
    build.add_argument("--jobs", "-j", type=int, help="parallel compile jobs")
    add_renode_options(build)


def build_parser() -> argparse.ArgumentParser:
    """A parser holding only these commands (``python -m jig.renode``)."""
    parser = argparse.ArgumentParser(prog="jig", description="Jig's Renode commands")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more logging")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True
    add_commands(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)-7s %(name)s: %(message)s")
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


# ------------------------------------------------------------------ running


def run(args: argparse.Namespace) -> int:
    """Run the command ``add_commands`` parsed into ``args``."""
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, _KEY, None)
    if handler is None:
        raise ConfigError("not a Renode command")
    try:
        return handler(args)
    except install.ConsentError as exc:
        _error(str(exc))
        return EXIT_USAGE
    except ConfigError as exc:
        _error(str(exc))
        return EXIT_USAGE
    except JigError as exc:
        _error(str(exc))
        return EXIT_INFRASTRUCTURE


def _error(text: str) -> None:
    print(f"error: {text}", file=sys.stderr)


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _config() -> discover.RenodeConfig | None:
    return discover.load_project_config()


def resolve_install(args: argparse.Namespace, *, offer_install: bool = True) -> RenodeInstall:
    """The Renode that ``--renode``, ``--allow-untested-renode``, ``jig.toml``
    and the environment select; at a terminal, offers ``jig renode install``
    when there is none."""
    explicit = getattr(args, "renode", None)
    allow = bool(getattr(args, "allow_untested_renode", False))
    config = _config()
    try:
        return discover.find_renode(explicit=explicit, config=config, allow_untested=allow)
    except JigError:
        if explicit is not None or not offer_install or not sys.stdin.isatty():
            raise
        asset = assets.find_asset()
        if not install.consent(asset, install.install_dir(asset)):
            raise
        install.install(asset, allow_download=True, progress=_say)
        return discover.find_renode(explicit=None, config=config, allow_untested=allow)


# ------------------------------------------------------------------ doctor


def _doctor(args: argparse.Namespace) -> int:
    report = doctor.run(
        explicit=args.renode,
        config=_config(),
        allow_untested=args.allow_untested_renode,
        boards=args.board,
    )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(report.text())
    return EXIT_OK if report.ok else EXIT_PROBLEMS


# ------------------------------------------------------------------ renode


def _renode_install(args: argparse.Namespace) -> int:
    asset = assets.find_asset(args.version, args.platform)
    target = install.install_dir(asset)
    existing = install.find_managed(asset)
    if existing is not None and not args.force:
        print(f"Renode {asset.version} ({asset.platform}) is installed: {existing.executable}")
        return EXIT_OK
    source: Path | None = args.source
    allowed = False
    if source is None and install.needs_download(asset):
        allowed = args.yes or install.consent(asset, target)
        if not allowed:
            raise install.ConsentError(
                f"installing Renode {asset.version} downloads {asset.size / 1e6:.1f} MB from "
                f"{asset.url}; agree at the prompt, pass --yes or set "
                f"{install.ENV_ASSUME_YES}=1, or pass --from with the archive"
            )
    elif source is not None:
        _say(install.LICENSE_NOTE)
    managed = install.install(
        asset, source=source, force=args.force, allow_download=allowed, progress=_say
    )
    if asset.platform == _host_platform():
        renode = discover.probe(managed.executable, source="managed")
        if renode.version != asset.version:
            raise JigError(
                f"{managed.executable} reports Renode {renode.version}, not {asset.version}"
            )
        print(f"{renode.describe()}")
    else:
        print(f"Renode {asset.version} ({asset.platform}): {managed.executable}")
    return EXIT_OK


def _host_platform() -> str | None:
    try:
        return assets.host_platform()
    except JigError:
        return None


def _renode_list(args: argparse.Namespace) -> int:
    rows = []
    for platform_key in assets.PLATFORMS:
        for managed in install.managed_installs(platform_key):
            rows.append(
                {
                    "version": managed.asset.version,
                    "platform": managed.asset.platform,
                    "executable": str(managed.executable),
                    "directory": str(managed.directory),
                    "sha256": managed.asset.sha256,
                }
            )
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print(f"no Jig-managed Renode in {install.renode_dir()}")
    else:
        for row in rows:
            print(f"{row['version']:<10} {row['platform']:<12} {row['executable']}")
    return EXIT_OK


def _renode_path(args: argparse.Namespace) -> int:
    renode = resolve_install(args, offer_install=False)
    print(renode.executable)
    _say(f"{renode.describe()} [{renode.status}, from {renode.source}]")
    return EXIT_OK


def _renode_remove(args: argparse.Namespace) -> int:
    asset = assets.find_asset(args.version, args.platform)
    removed = install.remove(asset)
    if not removed:
        print(
            f"Renode {asset.version} ({asset.platform}) is not installed in {install.renode_dir()}"
        )
        return EXIT_PROBLEMS
    for path in removed:
        print(f"removed {path}")
    return EXIT_OK


# ------------------------------------------------------------------ models


def _board_sets(boards: Sequence[str]) -> list[models.ModelSet]:
    return models.board_sets(boards)


def _models_check(args: argparse.Namespace) -> int:
    sets = _board_sets(args.board)
    renode = resolve_install(args, offer_install=False)
    if args.sdk:
        return _models_check_sdk(renode, [models.jig_model_set(), *sets])
    from jig.renode.process import RenodeProcess
    from jig.renode.session import compile_model_set

    workdir = Path(tempfile.mkdtemp(prefix="models-check-"))
    process = RenodeProcess(workdir=workdir, install=renode)
    failed = False
    try:
        try:
            process.start()
        except models.ModelCompileError as exc:
            print(f"FAIL  {models.JIG_SET}")
            print(str(exc))
            return EXIT_PROBLEMS
        jig_set = process.jig_models
        seconds = process.startup_compile_seconds or 0.0
        print(f"ok    {jig_set.name} ({len(jig_set.files)} files) {seconds:.2f} s")
        for model_set in sets:
            try:
                timing = compile_model_set(process.client, model_set, context=sets)
            except models.ModelCompileError as exc:
                failed = True
                print(f"FAIL  {model_set.name}")
                for diagnostic in exc.diagnostics or []:
                    print(f"      {diagnostic}")
                if not exc.diagnostics:
                    print(f"      {exc.text}")
                continue
            print(f"ok    {timing.name} ({timing.files} files) {timing.seconds:.2f} s")
    finally:
        process.stop()
        if not failed:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)
    return EXIT_PROBLEMS if failed else EXIT_OK


def _models_check_sdk(renode: RenodeInstall, sets: Sequence[models.ModelSet]) -> int:
    if renode.assemblies is None or not renode.has_assemblies:
        raise ConfigError(f"Renode.dll not found for {renode.executable}: --sdk needs it")
    build = models.sdk_build(sets, renode.assemblies, project.cache_dir() / "models" / "sdk")
    errors = [d for d in build.diagnostics if d.severity == "error"]
    for diagnostic in build.diagnostics:
        print(f"      {diagnostic}")
    names = " + ".join(s.name for s in sets)
    if build.ok:
        print(f"ok    {names}: SDK build, warnings as errors, {build.seconds:.2f} s")
        return EXIT_OK
    if not errors:
        print(build.output[-3000:])
    print(f"FAIL  SDK build of {' + '.join(s.name for s in sets)} ({build.project})")
    return EXIT_PROBLEMS


def _models_ide(args: argparse.Namespace) -> int:
    renode = resolve_install(args, offer_install=False)
    if renode.assemblies is None or not renode.has_assemblies:
        raise ConfigError(f"Renode.dll not found for {renode.executable}: the csproj needs it")
    sets = [models.jig_model_set()]
    if args.board is not None:
        sets += _board_sets([args.board])
    focus = sets[-1]
    name = "".join(part.capitalize() for part in focus.name.replace("_", "-").split("-")) or "Jig"
    output = args.output or (focus.root / f"{name}.Models.csproj")
    obj = project.cache_dir() / "models" / "ide" / name
    path = models.write_sdk_project(
        output,
        sets,
        renode.assemblies,
        assembly_name=f"{name}.Models",
        output_dir=obj,
        strict=False,
    )
    print(path)
    _say(f"{sum(len(s.files) for s in sets)} files from {', '.join(s.name for s in sets)}; "
         f"Renode assemblies from {renode.assemblies}")  # fmt: skip
    return EXIT_OK


# ------------------------------------------------------------------ cosim


def _cosim_build(args: argparse.Namespace) -> int:
    from jig.ext.cosim import AxiLitePort, CosimBuild, build_library

    parameters = {}
    for item in args.parameters:
        name, separator, value = item.partition("=")
        if not separator or not name:
            raise ConfigError(f"-G expects NAME=VALUE, got {item!r}")
        parameters[name] = value
    renode = (
        resolve_install(args, offer_install=False) if args.integration_library is None else None
    )
    spec = CosimBuild(
        top=args.top,
        sources=args.sources,
        output=args.output,
        root=args.root,
        clock_mhz=args.clock_mhz,
        clock_parameter=args.clock_param or None,
        parameters=parameters,
        interrupts=tuple(args.interrupts),
        port=AxiLitePort(prefix=args.prefix, clock=args.clock, reset=args.reset),
        cxx=tuple(args.cxx),
        include_dirs=tuple(args.include_dirs),
        verilator_args=tuple(args.verilator_arg),
        integration_library=args.integration_library,
        jobs=args.jobs,
    )
    started = time.monotonic()
    library = build_library(spec, renode=renode)
    print(library)
    _say(f"built in {time.monotonic() - started:.1f} s")
    return EXIT_OK
