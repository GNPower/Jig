"""RTL co-simulation: building Verilated models and attaching them to Renode.

A board option can put RTL in place of a peripheral model (``cosim`` in the
option's choice, see :class:`jig.system.boards.CosimSpec`). The RTL builds into
a shared library that Renode's ``CoSimulatedPeripheral`` loads in library mode:
the model runs inside the Renode process, clocked by the emulation, and the
peripheral forwards bus accesses and interrupts.

``build_library`` (``jig cosim build``) is the generic build, for a top
module whose Renode-facing port is AXI4-Lite with the IntegrationLibrary's
widths (64-bit address and data, 8-bit strobes) and an active-low reset:

1. Verilator compiles the design from its file list, with Jig's SystemVerilog
   templates on the library path (``templates/``: ``jig_axil_byte_lanes``,
   ``jig_axil_decoder``, ``jig_axil_regs``, ``jig_axil_bram``,
   ``jig_dp_bram``);
2. a private copy of Renode's IntegrationLibrary is made, with Renode
   1.17.0's ``connect()`` call fixed (see ``patch_integration_library``);
3. ``sim_main.cpp`` is generated from ``templates/sim_main.cpp.in``: the
   context-string parser, the AXI4-Lite binding and the interrupts;
4. the IntegrationLibrary sources, ``sim_main.cpp`` and any extra C++ sources
   compile and link into ``lib<top>.so``.

Verilator and the IntegrationLibrary run on Linux, so co-simulation does too.
``library`` caches a board's build by its inputs: the build script, every
file the Verilator file list names, the extra inputs, the clock and the tool
versions.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jig import artifacts, project, repl
from jig.errors import ConfigError, ToolchainError
from jig.renode.discover import RenodeInstall
from jig.renode.session import RenodeSession
from jig.renode.toolchain import RENODE_VERSION
from jig.system.boards import CosimSpec

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).resolve().parent / "templates"
"""Jig's SystemVerilog templates and the ``sim_main.cpp`` template."""

ENV_INTEGRATION_LIBRARY = "RENODE_INTEGRATION_LIBRARY"

# The IntegrationLibrary sources an AXI4-Lite target in library mode needs.
VENDOR_SOURCES = (
    "src/renode_bus.cpp",
    "src/buses/bus.cpp",
    "src/buses/axilite.cpp",
    "src/communication/socket_channel.cpp",
    "libs/socket-cpp/Socket/Socket.cpp",
    "libs/socket-cpp/Socket/TCPClient.cpp",
)

# Renode 1.17.0's renode_bus.cpp calls the three-argument
# SocketCommunicationChannel::connect, which its socket_channel.h replaced
# with connect(SocketConnectionArgs *): the shipped library does not compile.
_CONNECT_CALL = "channel->connect(receiverPort, senderPort, address);"
_CONNECT_FIX = (
    "SocketConnectionArgs args{receiverPort, senderPort, address, 0};\n    channel->connect(&args);"
)

AXI_LITE_SIGNALS = (
    "awaddr", "awprot", "awvalid", "awready", "wdata", "wstrb", "wvalid", "wready",
    "bresp", "bvalid", "bready", "araddr", "arprot", "arvalid", "arready",
    "rdata", "rresp", "rvalid", "rready",
)  # fmt: skip


@dataclass(frozen=True)
class AxiLitePort:
    """The names of the top's Renode-facing signals: ``<prefix><signal>`` for
    the 19 AXI4-Lite signals, plus the clock and the active-low reset."""

    prefix: str = ""
    clock: str = "clk"
    reset: str = "rst_n"

    def bindings(self) -> list[tuple[str, str]]:
        """(IntegrationLibrary field, top signal) pairs."""
        pairs = [("clk", self.clock), ("rst", self.reset)]
        pairs += [(name, f"{self.prefix}{name}") for name in AXI_LITE_SIGNALS]
        return pairs


@dataclass(frozen=True)
class CosimBuild:
    """What ``build_library`` builds."""

    top: str
    """The top module, Renode's peripheral."""
    sources: Path
    """A Verilator file list (``-f``)."""
    output: Path
    """The build directory; the library is ``<output>/lib<top>.so``."""
    root: Path | None = None
    """Where the file list's relative paths start (default: the list's directory)."""
    clock_mhz: int | None = None
    """The model clock, passed to the top as ``-G<clock_parameter>=<MHz>``."""
    clock_parameter: str | None = "CLK_MHZ"
    parameters: Mapping[str, str] = field(default_factory=dict)
    """More ``-G`` overrides of the top's parameters."""
    interrupts: tuple[str, ...] = ()
    """Top-level outputs that become the peripheral's GPIOs 0, 1, ..."""
    port: AxiLitePort = AxiLitePort()
    cxx: tuple[Path, ...] = ()
    """Extra C++ sources linked into the library (DPI-C functions, the
    ``jig_cosim_context`` hook)."""
    include_dirs: tuple[Path, ...] = ()
    verilator_args: tuple[str, ...] = ()
    integration_library: Path | None = None
    """Renode's IntegrationLibrary (default: ``$RENODE_INTEGRATION_LIBRARY``,
    else ``plugins/IntegrationLibrary`` in the Renode install)."""
    jobs: int | None = None


def _identifier(value: str, what: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ConfigError(f"{what} {value!r} is not a C++/SystemVerilog identifier")
    return value


def render_sim_main(spec: CosimBuild) -> str:
    """``sim_main.cpp`` for ``spec`` (see ``templates/sim_main.cpp.in``)."""
    template = (TEMPLATES / "sim_main.cpp.in").read_text(encoding="utf-8")
    top = _identifier(spec.top, "top module")
    bindings = "\n".join(
        f"    bus->{field_name} = &top->{_identifier(signal, 'signal')};"
        for field_name, signal in spec.port.bindings()
    )
    interrupts = "\n".join(
        f"    agent->registerInterrupt(&top->{_identifier(name, 'interrupt')}, {index});"
        for index, name in enumerate(spec.interrupts)
    )
    return (
        template.replace("@TOP@", top)
        .replace("@BINDINGS@", bindings)
        .replace("@INTERRUPTS@", interrupts or "    // no interrupts")
    )


def patch_integration_library(directory: Path) -> bool:
    """Fix the IntegrationLibrary copy in ``directory`` for Renode 1.17.0;
    return whether it needed the fix (a fixed release is left alone)."""
    source = directory / "src" / "renode_bus.cpp"
    text = source.read_text(encoding="utf-8")
    if _CONNECT_CALL not in text:
        return False
    source.write_text(text.replace(_CONNECT_CALL, _CONNECT_FIX), encoding="utf-8", newline="\n")
    return True


def integration_library(explicit: Path | None = None, renode: RenodeInstall | None = None) -> Path:
    """Renode's IntegrationLibrary: ``explicit``, ``$RENODE_INTEGRATION_LIBRARY``
    or ``plugins/IntegrationLibrary`` of the Renode install."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    elif os.environ.get(ENV_INTEGRATION_LIBRARY):
        candidates.append(Path(os.environ[ENV_INTEGRATION_LIBRARY]))
    else:
        if renode is None:
            from jig.renode.discover import find_renode, load_project_config

            renode = find_renode(explicit=None, config=load_project_config())
        candidates.append(renode.root / "plugins" / "IntegrationLibrary")
    for candidate in candidates:
        if (candidate / "src" / "renode_bus.cpp").is_file():
            return candidate
    raise ToolchainError(
        "Renode's IntegrationLibrary was not found (looked in "
        f"{', '.join(str(c) for c in candidates)}); set {ENV_INTEGRATION_LIBRARY}"
    )


def _run(command: Sequence[str], cwd: Path, log_file: Path) -> None:
    with log_file.open("a", encoding="utf-8") as output:
        output.write(f"$ {' '.join(command)}\n")
        output.flush()
        result = subprocess.run(
            list(command), cwd=cwd, stdout=output, stderr=subprocess.STDOUT, check=False
        )
    if result.returncode != 0:
        tail = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        raise ToolchainError(
            f"{command[0]} failed (exit {result.returncode}); see {log_file}:\n" + "\n".join(tail)
        )


def build_library(spec: CosimBuild, *, renode: RenodeInstall | None = None) -> Path:
    """Build ``spec`` into ``<output>/lib<top>.so`` and return its path."""
    if platform.system() != "Linux":
        raise ToolchainError(
            "RTL co-simulation builds on Linux (Verilator, Renode's IntegrationLibrary)"
        )
    for tool in ("verilator", "g++"):
        if shutil.which(tool) is None:
            raise ToolchainError(f"{tool} is not on PATH")
    top = _identifier(spec.top, "top module")
    sources = spec.sources.resolve()
    if not sources.is_file():
        raise ConfigError(f"Verilator file list {sources} not found")
    root = (spec.root or sources.parent).resolve()
    library_dir = integration_library(spec.integration_library, renode)
    out = spec.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    log_file = out / "build.log"
    log_file.write_text("", encoding="utf-8")
    parameters = dict(spec.parameters)
    if spec.clock_mhz is not None and spec.clock_parameter:
        parameters[_identifier(spec.clock_parameter, "clock parameter")] = str(spec.clock_mhz)
    jobs = str(spec.jobs or os.cpu_count() or 4)

    # Zero-initialised state and X assignments keep runs reproducible.
    verilate = [
        "verilator", "--cc", "--build", "-j", jobs, "-O3",
        "--x-assign", "0", "--x-initial", "0", "-Wno-fatal",
        "--top-module", top,
        *(f"-G{_identifier(name, 'parameter')}={value}" for name, value in parameters.items()),
        "--Mdir", str(out / "obj"), "-CFLAGS", "-fPIC -O2",
        "-y", str(TEMPLATES), "+libext+.sv",
        *spec.verilator_args,
        "-f", str(sources),
    ]  # fmt: skip
    log.info("verilating %s (log: %s)", top, log_file)
    _run(verilate, root, log_file)

    vendor = out / "integration"
    shutil.rmtree(vendor, ignore_errors=True)
    shutil.copytree(library_dir, vendor)
    if patch_integration_library(vendor):
        log.info("patched the IntegrationLibrary copy for Renode 1.17.0's connect()")
    sim_main = out / "sim_main.cpp"
    sim_main.write_text(render_sim_main(spec), encoding="utf-8", newline="\n")

    verilator_root = (
        os.environ.get("VERILATOR_ROOT")
        or subprocess.run(
            ["verilator", "--getenv", "VERILATOR_ROOT"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    flags = [
        "-std=c++17", "-O2", "-fPIC", f"-I{out / 'obj'}",
        "-isystem", f"{verilator_root}/include", "-isystem", f"{verilator_root}/include/vltstd",
        "-isystem", str(vendor), "-isystem", str(vendor / "src"),
        "-isystem", str(vendor / "libs" / "socket-cpp"),
        *(f"-I{directory.resolve()}" for directory in spec.include_dirs),
    ]  # fmt: skip
    objects_dir = out / "glue"
    objects_dir.mkdir(exist_ok=True)
    objects: list[str] = []
    own = [sim_main, *(path.resolve() for path in spec.cxx)]
    for index, source in enumerate([*(vendor / s for s in VENDOR_SOURCES), *own]):
        target = objects_dir / f"{index:02d}-{source.stem}.o"
        # The IntegrationLibrary builds as shipped; Jig's and the user's code with warnings on.
        warnings = ["-Wall", "-Wextra"] if source in own else ["-w"]
        _run(["g++", *flags, *warnings, "-c", str(source), "-o", str(target)], out, log_file)
        objects.append(str(target))
    library = out / f"lib{top}.so"
    model = [
        "-Wl,--whole-archive", str(out / "obj" / f"V{top}__ALL.a"), "-Wl,--no-whole-archive",
        str(out / "obj" / "libverilated.a"), "-lpthread", "-latomic",
    ]  # fmt: skip
    _run(["g++", "-shared", "-o", str(library), *objects, *model], out, log_file)
    log.info("built %s", library)
    return library


# ------------------------------------------------------- board options


def clock_hz(spec: CosimSpec, description: Path) -> int:
    """The model clock: the co-simulated peripheral's ``frequency`` in the
    platform description ``description``."""
    value = repl.properties(description).get(spec.peripheral, {}).get("frequency")
    if not isinstance(value, int) or value <= 0 or value % 1_000_000:
        raise ConfigError(
            f"{description.name}: {spec.peripheral}.frequency must be a whole number of MHz "
            f"(the RTL is built for that clock), got {value!r}"
        )
    return value


def listed_sources(file_list: Path, root: Path | None = None) -> list[Path]:
    """Files a Verilator file list names, relative to ``root`` (the board's
    root; the project root when ``None``).

    Options (``+define+``, ``-I``) and environment references (``$(NAME)``,
    generated files) are skipped; ``//`` comments are ignored."""
    base = root if root is not None else project.root()
    files = []
    for raw in file_list.read_text(encoding="utf-8").splitlines():
        line = raw.split("//", 1)[0].strip()
        for token in line.split():
            if token.startswith(("+", "-")) or token.startswith("$"):
                continue
            path = (base / token).resolve()
            if not path.is_file():
                raise ConfigError(f"{file_list}: {token} not found")
            files.append(path)
    return files


def spec_root(spec: CosimSpec) -> Path:
    """Where a board's co-simulation build runs and its file list starts: the
    board's root (``CosimSpec.root``, board.yaml schema 2), else the project root."""
    root: Path | None = getattr(spec, "root", None)
    return root if root is not None else project.root()


def _expand(inputs: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for path in inputs:
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            raise ConfigError(f"co-simulation input {path} not found")
    return files


def _tool_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ToolchainError(f"{command[0]} is not available: {exc}") from None
    if result.returncode != 0:
        return ""
    return (result.stdout or result.stderr).strip().splitlines()[0]


def library(spec: CosimSpec, description: Path) -> Path:
    """Build (or reuse) the library for ``spec`` at the clock the platform
    description ``description`` sets."""
    if platform.system() != "Linux":
        raise ToolchainError(
            "RTL co-simulation needs Linux (Verilator, GHDL, Renode's IntegrationLibrary)"
        )
    mhz = clock_hz(spec, description) // 1_000_000
    verilator = _tool_version(["verilator", "--version"])
    if not verilator:
        raise ToolchainError("verilator is not on PATH")
    root = spec_root(spec)
    inputs = [spec.build, spec.sources, *listed_sources(spec.sources, root), *_expand(spec.inputs)]
    templates = sorted(p for p in TEMPLATES.iterdir() if p.is_file())
    unique = list(dict.fromkeys([*inputs, *templates]))
    name = re.sub(r"[^A-Za-z0-9]+", "-", spec.library.removesuffix(".so")).strip("-")

    def build(directory: Path) -> None:
        log_path = directory / "build.log"
        log.info("building %s at %d MHz (log: %s)", spec.library, mhz, log_path)
        with log_path.open("w", encoding="utf-8") as output:
            result = subprocess.run(
                ["bash", str(spec.build), str(directory), "--clock-mhz", str(mhz)],
                stdout=output,
                stderr=subprocess.STDOUT,
                cwd=root,
                check=False,
            )
        if result.returncode != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
            raise ToolchainError(f"building {spec.library} failed:\n" + "\n".join(tail))
        if not (directory / spec.library).is_file():
            raise ToolchainError(f"{spec.build} did not produce {spec.library}")

    params = {"clock_mhz": mhz, "verilator": verilator, "renode": RENODE_VERSION}
    return artifacts.cached(f"cosim-{name}", unique, build, params) / spec.library


def attach(session: RenodeSession, spec: CosimSpec, path: Path, workdir: Path) -> None:
    """Load the library into the co-simulated peripheral of the selected machine.

    The context string reaches the model before it initialises, so it is set
    first; Renode refuses to change it once the library is loaded."""
    if spec.context:
        context = spec.context.format(workdir=workdir.as_posix())
        session.execute(f'{spec.peripheral} SimulationContextLinux "{context}"')
    session.execute(f"{spec.peripheral} SimulationFilePathLinux @{path.as_posix()}")
