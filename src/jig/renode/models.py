"""C# model sets and how they reach Renode.

Renode compiles C# itself (Roslyn, C# 12, ``Monitor.TryCompilePlugin``, the
code behind the monitor's ``include``), so a session needs no .NET SDK:

* Jig's own models (``jig/models/**/*.cs``, shipped in the package) compile
  in one include when Renode starts (``jig.renode.process``);
* each ``ModelSet`` a session is given then compiles in order, as one
  include, against the assemblies before it: Renode loads every compiled
  assembly into its process, and the compiler references every loaded one.

A compile error comes back as Roslyn's ``path(line,col): error CSxxxx:
message`` lines; ``ModelCompileError`` maps them to the set and to the file
within the set's root.

``write_sdk_project`` generates a csproj for the .NET SDK with HintPaths into
a Renode install: ``jig models check --sdk`` builds it with warnings as errors
(Renode's own compiler drops warnings) and ``jig models ide`` writes one next
to a board's models for an editor. ``tools/models/Jig.Models.csproj`` is the
same build for a Jig checkout.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from jig import project
from jig.errors import ConfigError, RenodeError, ToolchainError

JIG_SET = "jig"
"""The name of Jig's own model set."""

PROJECT_NAME = "Jig.Models"

# The Renode assemblies a model may call into (as in tools/models/Jig.Models.csproj).
SDK_REFERENCES = (
    "Infrastructure",
    "Renode",
    "Migrant",
    "ELFSharp",
    "AntShell",
    "System.Collections.Immutable",
)


@dataclass(frozen=True)
class ModelSet:
    """C# model sources that compile together, after Jig's own and any set before them.

    ``files`` are absolute or relative to ``root``; ``root`` also anchors the
    file names in compiler messages."""

    name: str
    root: Path
    files: tuple[Path, ...]

    def paths(self) -> tuple[Path, ...]:
        """The files as absolute paths; raises ``ConfigError`` for a missing or non-.cs file."""
        resolved = []
        for file in self.files:
            path = (file if file.is_absolute() else self.root / file).resolve()
            if path.suffix != ".cs":
                raise ConfigError(f"model set {self.name}: {path} is not a .cs file")
            if not path.is_file():
                raise ConfigError(f"model set {self.name}: {path} does not exist")
            resolved.append(path)
        if not resolved:
            raise ConfigError(f"model set {self.name} has no .cs files")
        return tuple(sorted(dict.fromkeys(resolved)))

    def display(self, path: Path | str) -> str:
        """``path`` relative to the set's root when it is inside it."""
        try:
            return Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(path)

    @classmethod
    def glob(cls, name: str, root: Path, patterns: Iterable[str]) -> ModelSet:
        """The files matching ``patterns`` (globs relative to ``root``, ``**`` allowed)."""
        files: list[Path] = []
        for pattern in patterns:
            matched = sorted(p for p in root.glob(pattern) if p.is_file() and p.suffix == ".cs")
            if not matched:
                raise ConfigError(f"model set {name}: {pattern!r} matches no .cs file in {root}")
            files.extend(matched)
        return cls(name=name, root=root, files=tuple(dict.fromkeys(files)))


def jig_models_dir() -> Path:
    """Jig's C# model sources (package data under ``jig/models``)."""
    return project.package_file("models")


def checkout_root() -> Path | None:
    """The Jig source checkout this package runs from (it holds
    ``tools/models/Jig.Models.csproj``, the SDK lint build), or ``None`` when
    the package is installed. Sessions need neither: Renode compiles the
    models itself."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / "tools" / "models" / f"{PROJECT_NAME}.csproj").is_file() else None


def jig_model_set() -> ModelSet:
    """Jig's own models: every .cs file the package ships."""
    root = jig_models_dir()
    return ModelSet(name=JIG_SET, root=root, files=tuple(sorted(root.rglob("*.cs"))))


def board_model_set(directory: Path, name: str | None = None) -> ModelSet:
    """The model set of a board directory.

    The ``models`` setting of its ``board.jig`` (globs relative to the board
    file), with the board's root as the set's root, when it has one;
    otherwise every .cs file under ``<directory>/models``, or under
    ``directory`` itself."""
    directory = directory.resolve()
    if not directory.is_dir():
        raise ConfigError(f"{directory} is not a directory")
    set_name = name or directory.name
    board_file = directory / "board.jig"
    if board_file.is_file():
        from jig.system import boards

        board = boards.load(board_file)
        if board.models:
            return ModelSet(name=name or board.name, root=board.root, files=board.models)
    models = directory / "models"
    root = models if models.is_dir() else directory
    files = tuple(sorted(root.rglob("*.cs")))
    if not files:
        raise ConfigError(f"{directory}: no C# models (no models setting, no .cs files)")
    return ModelSet(name=set_name, root=root, files=files)


def board_sets(boards: Iterable[str | Path]) -> list[ModelSet]:
    """The model sets of boards named by directory (``board_model_set``) or by
    name on the project's board search path (through
    ``jig.system.boards.model_sets``)."""
    sets: list[ModelSet] = []
    for board in boards:
        path = Path(board)
        if path.is_dir():
            sets.append(board_model_set(path))
            continue
        from jig.system import boards as board_files

        by_name = getattr(board_files, "model_sets", None)
        if by_name is None:
            raise ConfigError(f"{board}: no such board directory")
        sets.extend(by_name([board_files.find(str(board))]))
    names = [s.name for s in sets]
    if len(set(names)) != len(names):
        raise ConfigError(f"model set names must differ: {', '.join(names)}")
    return sets


# ---------------------------------------------------------------- diagnostics


@dataclass(frozen=True)
class ModelDiagnostic:
    """One compiler message, mapped to its model set."""

    set: str
    file: str
    """The file relative to its set's root (or as the compiler wrote it)."""
    line: int
    column: int
    severity: str
    code: str
    message: str

    def __str__(self) -> str:
        return (
            f"{self.set}: {self.file}({self.line},{self.column}): "
            f"{self.severity} {self.code}: {self.message}"
        )


_DIAGNOSTIC = re.compile(
    r"^\s*(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)(?:,\d+,\d+)?\):\s*"
    r"(?P<severity>error|warning)\s+(?P<code>[A-Z]+\d+):\s*(?P<message>.*?)"
    r"(?:\s+\[[^\]]*\.csproj\])?\s*$",
    re.MULTILINE,
)


def _key(path: str | Path) -> str:
    return os.path.normcase(Path(path).resolve())


def parse_diagnostics(
    text: str, sets: Sequence[ModelSet], default: str | None = None
) -> list[ModelDiagnostic]:
    """Compiler messages in ``text``, each mapped to the set holding its file."""
    owners: dict[str, ModelSet] = {}
    for model_set in sets:
        for file in model_set.files:
            path = file if file.is_absolute() else model_set.root / file
            owners.setdefault(_key(path), model_set)
    found: list[ModelDiagnostic] = []
    seen: set[tuple[str, int, int, str, str]] = set()
    for match in _DIAGNOSTIC.finditer(text):
        reported = match.group("path")
        owner = owners.get(_key(reported))
        identity = (_key(reported), int(match.group("line")), int(match.group("column")),
                    match.group("code"), match.group("message"))  # fmt: skip
        if identity in seen:  # MSBuild repeats each message in its summary
            continue
        seen.add(identity)
        found.append(
            ModelDiagnostic(
                set=owner.name if owner is not None else (default or "?"),
                file=owner.display(reported) if owner is not None else reported,
                line=int(match.group("line")),
                column=int(match.group("column")),
                severity=match.group("severity"),
                code=match.group("code"),
                message=match.group("message"),
            )
        )
    return found


class ModelCompileError(ToolchainError, RenodeError):
    """A model set did not compile (an infrastructure failure: exit code 3)."""

    def __init__(self, set_name: str, diagnostics: Sequence[ModelDiagnostic], text: str) -> None:
        self.set_name = set_name
        self.diagnostics = list(diagnostics)
        self.text = text.strip()
        errors = [d for d in self.diagnostics if d.severity == "error"] or self.diagnostics
        if errors:
            listing = "\n".join(f"  {d}" for d in errors[:20])
            more = f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else ""
            detail = f"{listing}{more}"
        else:
            detail = "  " + "\n  ".join(self.text.splitlines()[:20])
        super().__init__(f"model set {set_name} does not compile:\n{detail}")


# ---------------------------------------------------------------- SDK builds


def _relative_or_absolute(path: Path, base: Path) -> str:
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except ValueError:  # another drive on Windows
        return path.as_posix()


def write_sdk_project(
    path: Path,
    sets: Sequence[ModelSet],
    renode_dir: Path,
    *,
    assembly_name: str = PROJECT_NAME,
    output_dir: Path | None = None,
    strict: bool = True,
) -> Path:
    """Write a csproj compiling ``sets`` (Jig's first when listed) into one
    assembly against the Renode in ``renode_dir`` (the directory of Renode.dll).

    ``output_dir`` takes the build's bin and obj trees (default: next to the
    project); ``strict`` treats warnings as errors."""
    missing = [name for name in SDK_REFERENCES if not (renode_dir / f"{name}.dll").is_file()]
    if missing:
        raise ToolchainError(f"{renode_dir} lacks {', '.join(missing)}.dll: not a Renode install")
    base = path.parent
    compile_items = []
    for model_set in sets:
        for file in model_set.paths():
            link = f"{model_set.name}/{model_set.display(file)}"
            compile_items.append(
                f"    <Compile Include={quoteattr(_relative_or_absolute(file, base))} "
                f"Link={quoteattr(link)} />"
            )
    references = "\n".join(
        f"    <Reference Include={quoteattr(name)}>\n"
        f"      <HintPath>{escape((renode_dir / f'{name}.dll').as_posix())}</HintPath>\n"
        "      <Private>false</Private>\n"
        "    </Reference>"
        for name in SDK_REFERENCES
    )
    outputs = ""
    if output_dir is not None:
        # Before the SDK's props, which read BaseIntermediateOutputPath: set
        # after them, MSBuild warns (MSB3539) and NuGet writes to the old path.
        outputs = (
            "  <PropertyGroup>\n"
            f"    <BaseIntermediateOutputPath>{escape((output_dir / 'obj').as_posix())}/"
            "</BaseIntermediateOutputPath>\n"
            f"    <BaseOutputPath>{escape((output_dir / 'bin').as_posix())}/</BaseOutputPath>\n"
            "  </PropertyGroup>\n"
        )
    text = f"""<Project>
  <!-- Generated by Jig (jig.renode.models.write_sdk_project) for Renode at
       {escape(renode_dir.as_posix())}. Regenerate it rather than editing it. -->
{outputs}  <Import Project="Sdk.props" Sdk="Microsoft.NET.Sdk" />
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>{escape(assembly_name)}</AssemblyName>
    <LangVersion>12</LangVersion>
    <Nullable>disable</Nullable>
    <TreatWarningsAsErrors>{"true" if strict else "false"}</TreatWarningsAsErrors>
    <WarningLevel>4</WarningLevel>
    <NoWarn>$(NoWarn);CA1416</NoWarn>
    <MSBuildWarningsAsMessages>$(MSBuildWarningsAsMessages);MSB3277</MSBuildWarningsAsMessages>
    <Deterministic>true</Deterministic>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup>
{chr(10).join(compile_items)}
  </ItemGroup>
  <ItemGroup>
{references}
  </ItemGroup>
  <Import Project="Sdk.targets" Sdk="Microsoft.NET.Sdk" />
</Project>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@dataclass(frozen=True)
class SdkBuild:
    """The outcome of ``sdk_build``."""

    ok: bool
    seconds: float
    diagnostics: list[ModelDiagnostic]
    output: str
    project: Path


def dotnet() -> str | None:
    """The ``dotnet`` command, if a .NET SDK is installed."""
    return shutil.which("dotnet")


def sdk_build(sets: Sequence[ModelSet], renode_dir: Path, build_dir: Path) -> SdkBuild:
    """Build ``sets`` with the .NET SDK, warnings as errors, in ``build_dir``."""
    command = dotnet()
    if command is None:
        raise ToolchainError("the .NET SDK (dotnet) is not installed; it is needed only for --sdk")
    names = "\n".join(f"{s.name}:{p}" for s in sets for p in s.paths())
    digest = hashlib.sha256(f"{renode_dir}\n{names}".encode()).hexdigest()[:12]
    work = build_dir / digest
    project_file = write_sdk_project(
        work / "Check.csproj", sets, renode_dir, assembly_name="Jig.ModelCheck", output_dir=work
    )
    started = time.monotonic()
    result = subprocess.run(
        [command, "build", str(project_file), "-c", "Release", "-nologo", "-v", "q",
         "--disable-build-servers"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )  # fmt: skip
    seconds = time.monotonic() - started
    output = f"{result.stdout}\n{result.stderr}".strip()
    diagnostics = parse_diagnostics(output, sets)
    return SdkBuild(result.returncode == 0, seconds, diagnostics, output, project_file)
