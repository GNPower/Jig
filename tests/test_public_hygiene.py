"""Public hygiene: no file in this repository names the private projects Jig
was extracted from, or the names Jig used before its release.

The deny-list holds SHA-256 digests of lowercase words, so this file does not
itself name what it guards against. Each entry is (kind, length, digest):

* ``word``: a whole word (a run of letters and digits), in any case;
* ``WORD``: a whole word written exactly so (case-sensitive);
* ``head``: the first ``length`` characters of a word, in any case;
* ``stem``: the first ``length`` characters of a word, written all upper or
  all lower case (a stem does not match inside a mixed-case word);
* ``part``: any ``length`` characters inside a word, in any case;
* ``pair``: two words joined by one space, hyphen, underscore, dot or slash,
  in any case, hashed as ``"first second"``;
* ``dot``: a word right after a dot (a file suffix), in any case.

A digest for a new entry: ``python -c "import hashlib;
print(hashlib.sha256(b'word').hexdigest())"``. ``ALLOWED`` lists the lines that
must keep a match, by file and SHA-256 of the stripped line; a failure prints
both.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DENY: tuple[tuple[str, int, str], ...] = (
    ("word", 3, "2fcb6465ee5cbfa3567355b4f7abe6f03ea08e9283de9c834d1297b1df2c6288"),
    ("word", 4, "069bceb3cb1e79f0e4810af5ed309d7aa306f98c7fa7cd0ad2c9d5b13335d653"),
    ("word", 3, "8d2879f8b70960bb38cb9eeb42d50003ddf169edbd660194ce9952b8fb33668d"),
    ("word", 3, "2f0b17ddf2a976da82046be53bdd35297ae13cd1d36b0254e94e5d8db19a4b10"),
    ("word", 3, "ca3f67cfd416e76f63e399dcae7459b8923c95d53b3c6b3e4616e5945b08912a"),
    ("word", 5, "3ca994587db5867238abd694e730b4935e9121df351143b42c79415f9c7e308d"),
    ("word", 8, "39e2cbc76364b285443896ebb3b0a36420b80f4172c4150414ea13f35449f1ce"),
    ("word", 6, "dea6e2895ffaf19fd803a709c40a7bd851c25c7d5e5ad1859675b961ec0a30af"),
    ("WORD", 6, "2ac620c6a7e03d42cab0d35de06aa6f698120cebdc73e4b5828092e5633cd2cf"),
    ("stem", 3, "fe3edd0c82b94d1883b1616377e91f24cb3b9c87e5f8d9c70dd42d2b3062d839"),
    ("head", 5, "4fa1a13ac468ac495f3390e859d76d5e8ef49806815b45a21de7711bcc624194"),
    ("part", 7, "0a23a7eecbab3a6cf2eb959d601877092f5ab0bae6e7ebfb6c16a3755b526d1e"),
    ("part", 8, "3f71c5990be63d7b4dc84cc03e3ebdad40ef5afae126d38d530c29a2356fb6e3"),
    ("part", 9, "5d044054cd4826e3b33a483392b58714448f905dd1ec30977da7faf267b03f75"),
    ("pair", 5, "e47eaf01eaa5b25b3707ec40066e2df336caec3fc82f2c193b04e3b03362117a"),
    ("pair", 7, "e3445ba4f30a1236edb439e1371fc626b059cfcec24673779a34699a2f84593f"),
    ("dot", 3, "69b22a06ce5c5ca19c4c4a5e7e0a3eee4644d9cbbe3c7efe9a8e148be595294f"),
)

# (file relative to the repository root, SHA-256 of the stripped line)
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        # The managed-install marker's older name, adopted once (toolchain._LEGACY_MARKER).
        (
            "src/jig/renode/toolchain.py",
            "85b4345a3e70faf266b9e60eae7abd5457cb8939c836d59592da8d75f8c08c3c",
        ),
    }
)

# Directories a fallback walk skips (git ls-files skips them through .gitignore).
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "build",
        "dist",
        "out",
        "bin",
        "obj",
        "node_modules",
    }
)

_TOKEN = re.compile(r"[A-Za-z0-9]+")
_SEPARATORS = frozenset(" -_./")


@functools.cache
def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Hit:
    line: int
    kind: str
    text: str
    source: str


def scan(text: str, deny: tuple[tuple[str, int, str], ...] = DENY) -> Iterator[Hit]:
    """Every deny-list match in ``text``."""
    words = {d for kind, _n, d in deny if kind == "word"}
    exact = {d for kind, _n, d in deny if kind == "WORD"}
    dots = {d for kind, _n, d in deny if kind == "dot"}
    pairs = {d for kind, _n, d in deny if kind == "pair"}
    heads = [(n, d) for kind, n, d in deny if kind == "head"]
    stems = [(n, d) for kind, n, d in deny if kind == "stem"]
    parts = [(n, d) for kind, n, d in deny if kind == "part"]
    for number, line in enumerate(text.splitlines(), 1):
        tokens = list(_TOKEN.finditer(line))
        for index, match in enumerate(tokens):
            word = match.group()
            lower = word.lower()
            found: list[tuple[str, str]] = []
            if _digest(lower) in words:
                found.append(("word", word))
            if _digest(word) in exact:
                found.append(("WORD", word))
            if match.start() > 0 and line[match.start() - 1] == "." and _digest(lower) in dots:
                found.append(("dot", "." + word))
            found += [("head", word) for n, d in heads if _digest(lower[:n]) == d]
            found += [
                ("stem", word)
                for n, d in stems
                if (word[:n].isupper() or word[:n].islower()) and _digest(lower[:n]) == d
            ]
            for n, d in parts:
                if any(_digest(lower[i : i + n]) == d for i in range(len(lower) - n + 1)):
                    found.append(("part", word))
            if index + 1 < len(tokens):
                after = tokens[index + 1]
                gap = line[match.end() : after.start()]
                if gap in _SEPARATORS and _digest(f"{lower} {after.group().lower()}") in pairs:
                    found.append(("pair", line[match.start() : after.end()]))
            for kind, shown in found:
                yield Hit(number, kind, shown, line.strip())


def _git_files() -> list[str] | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return [name for name in result.stdout.decode("utf-8", "surrogateescape").split("\0") if name]


def _walked_files() -> list[str]:
    names = []
    for directory, subdirectories, files in os.walk(ROOT):
        subdirectories[:] = [
            d for d in subdirectories if d not in SKIP_DIRS and not d.endswith(".egg-info")
        ]
        for name in files:
            names.append((Path(directory) / name).relative_to(ROOT).as_posix())
    return names


def repository_files() -> list[str]:
    """Files git tracks or would track (tracked plus untracked, not ignored)."""
    names = _git_files()
    if names is None:
        names = _walked_files()
    return sorted(name for name in names if (ROOT / name).is_file())


def _text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def test_the_scanner_matches_each_kind() -> None:
    deny = (
        ("word", 5, _digest("zebra")),
        ("WORD", 4, _digest("LION")),
        ("head", 4, _digest("hipp")),
        ("stem", 3, _digest("emu")),
        ("part", 3, _digest("gnu")),
        ("pair", 9, _digest("red panda")),
        ("dot", 3, _digest("owl")),
    )
    text = (
        "A Zebra and a zebras.\n"
        "LION, but not Lion.\n"
        "hippopotamus; ship\n"
        "EMUs, emu_farm, but not EmUlate\n"
        "the_gnus sang\n"
        "red-panda, red_panda, red  panda\n"
        "notes.owl and owl\n"
    )
    hits = [(hit.line, hit.kind, hit.text) for hit in scan(text, deny)]
    assert hits == [
        (1, "word", "Zebra"),
        (2, "WORD", "LION"),
        (3, "head", "hippopotamus"),
        (4, "stem", "EMUs"),
        (4, "stem", "emu"),
        (5, "part", "gnus"),
        (6, "pair", "red-panda"),
        (6, "pair", "red_panda"),
        (7, "dot", ".owl"),
    ]


def test_a_register_name_holding_a_denied_word_passes() -> None:
    # ST's FDCAN_XIDAM (RM0456) ends in a denied word: the whole-word entry
    # leaves the register name alone and still catches the word on its own.
    assert list(scan("FDCAN_XIDAM = 0x1FFFFFFF")) == []
    assert [hit.kind for hit in scan("XIDAM"[2:])] == ["word"]


def test_the_listing_covers_the_repository() -> None:
    names = set(repository_files())
    assert {"pyproject.toml", "src/jig/cli.py", "tests/test_public_hygiene.py"} <= names
    assert not any(name.startswith((".git/", ".venv/")) for name in names)


def test_no_file_names_a_private_project() -> None:
    problems = []
    for name in repository_files():
        text = _text(ROOT / name)
        if text is None:
            continue
        for hit in scan(text):
            if (name, _digest(hit.source)) in ALLOWED:
                continue
            problems.append(
                f"{name}:{hit.line}: {hit.kind} {hit.text!r} (line digest {_digest(hit.source)})"
            )
    assert problems == [], "\n".join(problems)
