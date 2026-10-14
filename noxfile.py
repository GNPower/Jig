"""The checks CI runs (``.github/workflows``), as nox sessions.

Each session runs on Windows, Linux and macOS unless it says otherwise::

    nox -s lint           ruff check, ruff format --check, PyMarkdown, zizmor
    nox -s typecheck      mypy --strict over the package, the model tests and this file
    nox -s tests-3.13     unit tests with coverage (tests-3.11 to tests-3.14)
    nox -s renode         the Renode tier: model and bridge tests on a real Renode
    nox -s cosim          RTL co-simulation on Renode (Linux with Verilator)
    nox -s example        the ZedBoard example's plans on Renode (needs arm-none-eabi-gcc)
    nox -s docs           sphinx-build -W --keep-going -E
    nox -s dist           build, twine check --strict, then the wheel in a clean venv
    nox -s hygiene        the public-hygiene test
    nox -s audit          pip-audit over the runtime dependencies

``nox`` alone runs lint, typecheck, tests (on every listed Python it finds),
docs and hygiene. The Renode sessions use ``JIG_RENODE`` or the Renode Jig's
discovery finds. With none, they run ``jig renode install``, which asks first
(``--yes`` when the ``CI`` variable is set).

Three sessions serve the workflows and need no virtual environment:
``renode-key`` (the cache key of the pinned Renode release), ``release-version``
(the version a release publishes) and ``verify`` (a published release,
installed from PyPI or TestPyPI and smoke-tested like ``dist``).
"""

from __future__ import annotations

import json
import os
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import nox
from packaging.version import Version

nox.needs_version = ">=2026.8.17"
nox.options.sessions = ["lint", "typecheck", "tests", "docs", "hygiene"]
nox.options.download_python = "never"  # a missing Python is an error, never a download

ROOT = Path(__file__).resolve().parent
PYPROJECT = nox.project.load_toml(ROOT / "pyproject.toml")
PYTHONS = ["3.11", "3.12", "3.13", "3.14"]
WINDOWS = sys.platform == "win32"

# Unit-test coverage (line and branch) never drops below this, in percent.
COVERAGE_FLOOR = 82

# Markdown that PyMarkdown checks: the root files and every .md under these.
MARKDOWN_TREES = ("docs", ".github", "examples", "src", "tests", "tools")
MARKDOWN_SKIP_DIRS = frozenset({"_build", ".venv", ".nox", "node_modules", "out", "build"})

INDEXES = {
    "pypi": "https://pypi.org/simple/",
    "testpypi": "https://test.pypi.org/simple/",
}


def _extra(name: str) -> list[str]:
    """The requirements of one extra in pyproject.toml, for sessions that
    need the tools but not the package."""
    requirements: list[str] = PYPROJECT["project"]["optional-dependencies"][name]
    return requirements


def _version() -> str:
    version: str = PYPROJECT["project"]["version"]
    return version


def _markdown_files() -> list[str]:
    files = sorted(path.name for path in ROOT.glob("*.md"))
    for tree in MARKDOWN_TREES:
        for path in sorted((ROOT / tree).rglob("*.md")):
            relative = path.relative_to(ROOT)
            if MARKDOWN_SKIP_DIRS.isdisjoint(relative.parts[:-1]):
                files.append(relative.as_posix())
    return files


# ---------------------------------------------------------------- checks


@nox.session
def lint(session: nox.Session) -> None:
    """ruff check, ruff format --check, PyMarkdown (.markdownlint.json's rules)
    and zizmor's security audit of the workflows (offline: no GitHub API)."""
    session.install(*_extra("lint"))
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")
    session.run("pymarkdown", "--strict-config", "scan", *_markdown_files())
    session.run("zizmor", "--offline", "--no-progress", ".github/workflows")


@nox.session
def typecheck(session: nox.Session) -> None:
    """mypy --strict over the package, then the model tests, this file and docs/conf.py."""
    session.install("-e", ".[typecheck]")
    session.run("mypy")  # [tool.mypy] packages: jig
    session.run("mypy", "tests/models", "noxfile.py", "docs/conf.py")


@nox.session(python=PYTHONS)
def tests(session: nox.Session) -> None:
    """The unit tests (no Renode), with line and branch coverage in coverage.xml."""
    session.install("-e", ".[test]")
    session.run(
        "pytest",
        "--cov",
        "--cov-report=term",
        "--cov-report=xml",
        f"--cov-fail-under={COVERAGE_FLOOR}",
        *session.posargs,
    )


@nox.session
def renode(session: nox.Session) -> None:
    """Jig's C# models compiled in Renode (and by the .NET SDK, warnings as
    errors, when dotnet is installed), then every test marked renode or slow."""
    session.install("-e", ".[test]")
    _use_renode(session, _venv_jig(session))
    session.run("jig", "doctor")
    session.run("jig", "models", "check")
    if shutil.which("dotnet"):
        session.run("jig", "models", "check", "--sdk")
    else:
        session.log("dotnet not found: skipping jig models check --sdk")
    session.run(
        "pytest",
        "-m",
        "renode or slow",
        "-rs",
        "--cov",
        "--cov-report=xml",
        "--junitxml=out/pytest-renode.xml",
        *session.posargs,
    )


@nox.session
def cosim(session: nox.Session) -> None:
    """A Verilated AXI4-Lite block built by ``jig cosim build`` and run on a
    Zynq-7000 machine in Renode. Linux with Verilator only."""
    if platform.system() != "Linux":
        session.skip("RTL co-simulation runs on Linux only")
    if shutil.which("verilator") is None:
        session.error("verilator is not on PATH (apt-get install verilator)")
    session.install("-e", ".[test]")
    _use_renode(session, _venv_jig(session))
    session.run(
        "pytest",
        "-m",
        "renode",
        "-rs",
        "--junitxml=out/pytest-cosim.xml",
        "tests/test_cosim_build.py",
        *session.posargs,
    )


@nox.session
def example(session: nox.Session) -> None:
    """The ZedBoard example: its firmware built with arm-none-eabi-gcc
    (ARM_NONE_EABI_GCC, else PATH). jig check and jig check --contracts on
    its boards, the Renode tests that need its ELFs, then every plan in
    examples/zedboard/plans run twice on Renode, which also checks that each
    gives one fingerprint. Reports and run directories go to out/example."""
    session.install("-e", ".[test]")
    _use_renode(session, _venv_jig(session))
    session.run("python", "examples/zedboard/firmware/build.py")
    with session.chdir(ROOT / "examples" / "zedboard"):  # jig check finds jig.toml from here
        session.run("jig", "check")
        session.run("jig", "check", "--contracts")
    session.run(
        "pytest", "-m", "firmware", "-rs", "--junitxml=out/pytest-example.xml",
        "tests/test_zedboard_example.py",
    )  # fmt: skip
    plans = sorted((ROOT / "examples" / "zedboard" / "plans").glob("*.jig"))
    out = ROOT / "out" / "example"
    session.run(
        "jig", "run", *(str(plan) for plan in plans), "--repeat", "2",
        "--junit", str(out / "junit.xml"), "--html", str(out / "report.html"),
        "--json", str(out / "summary.json"),
        env={"JIG_OUTPUT": str(out)},
    )  # fmt: skip


@nox.session
def docs(session: nox.Session) -> None:
    """The documentation, failing on any warning."""
    session.install("-e", ".[docs]")
    session.run("sphinx-build", "-W", "--keep-going", "-E", "docs", "docs/_build/html")


@nox.session
def hygiene(session: nox.Session) -> None:
    """The public-hygiene test alone: no file names a private project. The
    test holds SHA-256 digests of the words it looks for, never the words."""
    session.install(*_extra("test"))
    session.run("pytest", "--noconftest", "-p", "no:cacheprovider", "tests/test_public_hygiene.py")


@nox.session
def audit(session: nox.Session) -> None:
    """pip-audit, against PyPI's vulnerability data, over every runtime
    dependency pip resolves for this project on this Python."""
    session.install(*_extra("audit"))
    with tempfile.TemporaryDirectory(prefix="jig-audit-") as temp:
        report = Path(temp) / "resolution.json"
        session.run(
            "python", "-m", "pip", "install", "--quiet", "--dry-run", "--ignore-installed",
            "--report", str(report), ".",
        )  # fmt: skip
        resolved = [entry["metadata"] for entry in json.loads(report.read_text("utf-8"))["install"]]
        pins = sorted(
            f"{item['name']}=={item['version']}"
            for item in resolved
            if item["name"].lower() != "jig-dsl"
        )
        requirements = Path(temp) / "requirements.txt"
        requirements.write_text("\n".join(pins) + "\n", encoding="utf-8")
        session.log(f"auditing {len(pins)} packages: {', '.join(pins)}")
        session.run(
            "pip-audit", "--progress-spinner", "off", "--no-deps", "--disable-pip",
            "--requirement", str(requirements),
        )  # fmt: skip


# ---------------------------------------------------------- distribution


@nox.session
def dist(session: nox.Session) -> None:
    """Build the sdist and, from it, the wheel. Check both with twine, then
    install the wheel in a clean venv outside the checkout and run Jig from
    there on Renode."""
    session.install(*_extra("release"))
    output = ROOT / "dist"
    shutil.rmtree(output, ignore_errors=True)
    session.run("python", "-m", "build", "--outdir", str(output))
    files = sorted(output.iterdir())
    session.run("twine", "check", "--strict", *(str(path) for path in files))
    (wheel,) = output.glob("*.whl")
    _smoke(session, "python", wheel, _version())


@nox.session(python=False)
def verify(session: nox.Session) -> None:
    """A published release, installed from its index into a clean venv and
    smoke-tested like ``dist``: ``nox -s verify -- pypi 0.1.0`` or
    ``-- testpypi 0.1.0a1``. Retries for 10 minutes while the index catches up."""
    if len(session.posargs) != 2 or session.posargs[0] not in INDEXES:
        session.error("usage: nox -s verify -- {pypi|testpypi} VERSION")
    index, version = session.posargs
    with tempfile.TemporaryDirectory(prefix="jig-download-") as download:
        # Only jig-dsl comes from the index named, its dependencies come from
        # PyPI when the wheel installs, so TestPyPI never supplies them.
        pip = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:"]
        _retry(
            session,
            [*pip, "--index-url", INDEXES[index], "--dest", download, f"jig-dsl=={version}"],
        )
        (wheel,) = Path(download).glob("*.whl")
        _smoke(session, sys.executable, wheel, version)


def _retry(session: nox.Session, command: list[str], attempts: int = 20) -> None:
    for attempt in range(1, attempts + 1):
        if subprocess.run(command, check=False).returncode == 0:
            return
        if attempt < attempts:
            session.log(f"attempt {attempt} of {attempts} failed; retrying in 30 s")
            time.sleep(30)
    session.error(f"failed {attempts} times: {' '.join(command)}")


def _smoke(session: nox.Session, base: str, wheel: Path, version: str) -> None:
    """Install ``wheel`` into a new venv (made by the ``base`` Python) in a
    temporary directory and run jig from there: --version, doctor (Renode
    starts and compiles Jig's models from site-packages), boards and list,
    and the fixture plan on Renode with every report."""
    with tempfile.TemporaryDirectory(prefix="jig-smoke-", ignore_cleanup_errors=True) as temp:
        work = Path(temp)
        # The fixture project, with a stub where example-node's ELF goes: the
        # board stays unpowered, so the file is checked but never loaded.
        fixtures = work / "fixtures"
        shutil.copytree(ROOT / "tests" / "fixtures", fixtures)
        (fixtures / "build").mkdir(exist_ok=True)
        (fixtures / "build" / "node.elf").write_bytes(b"\x7fELF")
        session.run(base, "-m", "venv", str(work / "venv"), external=True)
        scripts = work / "venv" / ("Scripts" if WINDOWS else "bin")
        python = str(scripts / "python")
        jig = str(scripts / "jig")
        session.run(python, "-m", "pip", "install", "--quiet", str(wheel), external=True)
        # The installed jig finds Renode (or installs it) in the usual cache.
        # the runs below get a cache and an output directory of their own.
        renode = _use_renode(session, jig)
        env = {
            "JIG_RENODE": renode,
            "JIG_PROJECT": str(fixtures),
            "JIG_CACHE_DIR": str(work / "cache"),
            "JIG_OUTPUT": str(work / "out"),
        }
        with session.chdir(work):
            shown = _output(session, jig, "--version", env=env)
            if shown.strip() != f"jig-dsl {version}":
                session.error(f"jig --version printed {shown!r}, not 'jig-dsl {version}'")
            imported = _output(session, python, "-c", "import jig; print(jig.__file__)")
            if Path(imported.strip()).is_relative_to(ROOT):
                session.error(f"jig imported from the checkout: {imported.strip()}")
            doctor = json.loads(_output(session, jig, "-q", "doctor", "--json", env=env))
            if not doctor["ok"]:
                session.error(f"jig doctor failed:\n{json.dumps(doctor, indent=2)}")
            session.run(jig, "boards", external=True, env=env)
            plan = str(fixtures / "plans" / "smoke.jig")
            session.run(jig, "list", plan, external=True, env=env)
            reports = work / "reports"
            session.run(
                jig,
                "run",
                plan,
                "--json",
                str(reports / "summary.json"),
                "--junit",
                str(reports / "junit.xml"),
                "--html",
                str(reports / "report.html"),
                external=True,
                env=env,
            )
            summary = json.loads((reports / "summary.json").read_text(encoding="utf-8"))
            (run,) = summary["plans"]
            if run["status"] != "PASS" or not (run.get("renode") or {}).get("version"):
                session.error(f"the smoke plan's summary is wrong:\n{json.dumps(run, indent=2)}")
            session.log(
                f"smoke plan: {run['status']} on Renode {run['renode']['version']}, "
                f"fingerprint {run['fingerprint'][:16]}"
            )


def _output(session: nox.Session, *command: str, env: dict[str, str] | None = None) -> str:
    """What ``command`` prints on stdout, its stderr goes to the log."""
    shown = session.run(*command, external=True, silent=True, stderr=None, env=env)
    return shown if isinstance(shown, str) else ""


# ---------------------------------------------------------------- Renode


def _venv_jig(session: nox.Session) -> str:
    jig = shutil.which("jig", path=session.bin)
    if jig is None:
        session.error(f"no jig in {session.bin}")
    return jig


def _renode_path(jig: str) -> str | None:
    """The Renode ``jig renode path`` reports, or None when it finds none."""
    result = subprocess.run(
        [jig, "renode", "path"], capture_output=True, text=True, check=False, env=os.environ
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip().splitlines()[-1]


def _use_renode(session: nox.Session, jig: str) -> str:
    """Point JIG_RENODE at a Renode for the rest of the session and return it:
    JIG_RENODE when set, else what ``jig``'s discovery finds, else a fresh
    ``jig renode install`` (which asks first; ``--yes`` when CI is set)."""
    explicit = os.environ.get("JIG_RENODE")
    found = _renode_path(jig)
    if found is None and explicit:
        session.error(f"JIG_RENODE={explicit} is not a usable Renode, check `jig renode path` for details")
    if found is None:
        command = [jig, "renode", "install"] + (["--yes"] if os.environ.get("CI") else [])
        session.run(*command, external=True)
        found = _renode_path(jig)
        if found is None:
            session.error("jig renode install finished, but jig renode path finds no Renode")
    session.env["JIG_RENODE"] = found
    session.log(f"Renode: {found}")
    return found


@nox.session(name="renode-key", python=False)
def renode_key(session: nox.Session) -> None:
    """The cache of the Renode release Jig pins, for actions/cache: sets
    JIG_CACHE_DIR to $RUNNER_TEMP/jig-cache for the job and writes the install
    directory (``path``) and a key holding the release's SHA-256 (``key``) to
    $GITHUB_OUTPUT."""
    sys.path.insert(0, str(ROOT / "src"))  # jig.errors, which the table imports
    assets = runpy.run_path(str(ROOT / "src" / "jig" / "renode" / "assets.py"))
    asset = assets["find_asset"](assets["DEFAULT_VERSION"], assets["host_platform"]())
    cache = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "jig-cache"
    values = {
        "path": (cache / "renode" / asset.key).as_posix(),
        "key": f"renode-{asset.key}-{asset.sha256}",
    }
    for name, value in values.items():
        session.log(f"{name}={value}")
    _github_file("GITHUB_ENV", {"JIG_CACHE_DIR": cache.as_posix()})
    _github_file("GITHUB_OUTPUT", values)


# --------------------------------------------------------------- release


@nox.session(name="release-version", python=False)
def release_version(session: nox.Session) -> None:
    """The version a release publishes, written to $GITHUB_OUTPUT as
    ``version`` and ``prerelease``.

    ``-- --tag vX.Y.Z`` fails unless the tag names pyproject.toml's version.
    ``-- --dev N`` rewrites that version to a development release unique to
    run N (``0.1.0.dev0`` becomes ``0.1.0.devN``, ``0.1.0a1`` becomes
    ``0.1.0a1.devN``), for a TestPyPI rehearsal: an index accepts each version
    once."""
    arguments = dict(zip(session.posargs[::2], session.posargs[1::2], strict=True))
    unknown = set(arguments) - {"--tag", "--dev"}
    if unknown or len(session.posargs) % 2:
        session.error("usage: nox -s release-version -- [--tag vX.Y.Z] [--dev N]")
    version = Version(_version())
    if "--dev" in arguments:
        stem = re.sub(r"\.dev\d+$", "", str(version))
        version = Version(f"{stem}.dev{int(arguments['--dev'])}")
        pyproject = ROOT / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        text, count = re.subn(
            r'^version = "[^"]*"$', f'version = "{version}"', text, count=1, flags=re.MULTILINE
        )
        if count != 1:
            session.error("no version line in pyproject.toml")
        pyproject.write_text(text, encoding="utf-8", newline="\n")
    tag = arguments.get("--tag")
    if tag is not None and tag != f"v{version}":
        session.error(
            f"the release tag {tag} does not match the version in pyproject.toml, {version}"
        )
    values = {"version": str(version), "prerelease": str(version.is_prerelease).lower()}
    for name, value in values.items():
        session.log(f"{name}={value}")
    _github_file("GITHUB_OUTPUT", values)


def _github_file(variable: str, values: dict[str, str]) -> None:
    """Append ``name=value`` lines to the file a workflow variable names, if set."""
    target = os.environ.get(variable)
    if target:
        with Path(target).open("a", encoding="utf-8", newline="\n") as handle:
            for name, value in values.items():
                handle.write(f"{name}={value}\n")


if __name__ == "__main__":  # pragma: no cover
    sys.exit("run this file with nox")
