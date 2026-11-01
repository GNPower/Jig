"""The .jig Pygments lexer: token kinds, error marking, and every plan snippet
in the documentation lexing without an error token."""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest

pygments_token = pytest.importorskip("pygments.token")

from jig.lang.pygments_lexer import JigLexer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
Token = pygments_token.Token

_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*jig[ \t]*$")


def _tokens(source: str) -> list[tuple[str, str]]:
    """(token type, text) pairs, blanks left out."""
    return [
        (str(kind), text)
        for kind, text in JigLexer().get_tokens(source)
        if text.strip() and kind not in Token.Text.Whitespace
    ]


def _kind_of(source: str, text: str) -> str:
    kinds = [kind for kind, value in _tokens(source) if value == text]
    assert kinds, f"{text!r} not lexed as one token in {source!r}"
    return kinds[0]


def _errors(source: str) -> list[str]:
    return [text for kind, text in JigLexer().get_tokens(source) if kind in Token.Error]


def documented_snippets() -> list[tuple[str, int, str]]:
    """(file, first line, source) of every ```jig block in the docs and README,
    and every .jig file under examples/."""
    snippets: list[tuple[str, int, str]] = []
    pages = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for page in pages:
        if not page.is_file() or "_build" in page.parts:
            continue
        lines = page.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            opening = _FENCE.match(lines[index])
            index += 1
            if opening is None:
                continue
            fence = opening.group("fence")
            start = index
            while index < len(lines) and not (
                lines[index].strip().startswith(fence) and set(lines[index].strip()) == {fence[0]}
            ):
                index += 1
            body = "\n".join(lines[start:index]) + "\n"
            snippets.append((page.relative_to(ROOT).as_posix(), start + 1, body))
            index += 1
    examples = ROOT / "examples"
    if examples.is_dir():
        for plan in sorted(examples.rglob("*.jig")):
            snippets.append(
                (plan.relative_to(ROOT).as_posix(), 1, plan.read_text(encoding="utf-8"))
            )
    return snippets


def test_the_entry_point_names_the_lexer() -> None:
    try:
        metadata.distribution("jig-dsl")
    except metadata.PackageNotFoundError:
        pytest.skip("jig-dsl is not installed, so its entry points are not registered")
    lexers = metadata.entry_points(group="pygments.lexers")
    assert [entry.value for entry in lexers if entry.name == "jig"] == [
        "jig.lang.pygments_lexer:JigLexer"
    ]
    from pygments.lexers import get_lexer_by_name, get_lexer_for_filename

    assert isinstance(get_lexer_by_name("jig"), JigLexer)
    assert isinstance(get_lexer_for_filename("plans/smoke.jig"), JigLexer)


def test_keywords_and_declarations() -> None:
    source = 'test "boot" [smoke] {\n    let x = 1\n    if x == 1 { log "one" }\n}\n'
    assert _kind_of(source, "test") == "Token.Keyword.Declaration"
    assert _kind_of(source, "let") == "Token.Keyword"
    assert _kind_of(source, "if") == "Token.Keyword"
    assert _kind_of(source, "log") == "Token.Keyword"
    system = (
        'system "bench" {\n    board dut: "zedboard" (node: 1)\n'
        "    node gs: csp(address: 10) over kiss\n"
        "    link ground: uart(members: [dut.kiss, gs])\n}\n"
    )
    assert _kind_of(system, "system") == "Token.Keyword.Declaration"
    assert _kind_of(system, "board") == "Token.Keyword.Declaration"
    assert _kind_of(system, "dut") == "Token.Name.Variable"
    assert _kind_of(system, "csp") == "Token.Name.Builtin"
    assert _kind_of(system, "over") == "Token.Operator.Word"
    assert _kind_of(system, "link") == "Token.Keyword.Declaration"
    assert _kind_of(system, "ground") == "Token.Name.Variable"


def test_board_file_declarations() -> None:
    source = (
        'board "zedboard" {\n'
        "    option node: choice(1, 2)\n"
        '    device zynq: zynq7000(part: "XC7Z020-CLG484") {\n'
        "        boot jtag\n"
        "        protocol csp(address: node)\n"
        "        interface can0: csp over cfp\n"
        "        unmodelled dsp: 0x43C00000 .. 0x43C0FFFF\n"
        "    }\n"
        '    peripheral flash0: "SPI.Micron_MT25Q" (size: 32 MiB)\n'
        "    external link can: can(bitrate: 1 Mbit/s, members: [zynq.can0])\n"
        "}\n"
    )
    assert _errors(source) == []
    for word in ("board", "option", "device", "boot", "protocol", "interface", "unmodelled"):
        assert _kind_of(source, word) == "Token.Keyword.Declaration", word
    assert _kind_of(source, "external") == "Token.Keyword.Declaration"
    assert _kind_of(source, "jtag") == "Token.Name.Variable"
    assert _kind_of(source, "zynq7000") == "Token.Name.Builtin"
    assert _kind_of(source, "MiB") == "Token.Keyword.Type"


def test_declaration_words_name_variables_elsewhere() -> None:
    source = "procedure restart(board, test) {\n    board.reset()\n    return test\n}\n"
    assert _kind_of(source, "procedure") == "Token.Keyword.Declaration"
    assert _kind_of(source, "restart") == "Token.Name.Function"
    assert {kind for kind, text in _tokens(source) if text in ("board", "test")} == {"Token.Name"}


def test_quantities_carry_their_units() -> None:
    source = "wait 100 ms\nlet rate = 1 Mbit/s\nlet v = 1.25 V\nlet n = 0x1F\n"
    assert _kind_of(source, "100") == "Token.Literal.Number.Integer"
    assert _kind_of(source, "ms") == "Token.Keyword.Type"
    assert _kind_of(source, "Mbit/s") == "Token.Keyword.Type"
    assert _kind_of(source, "1.25") == "Token.Literal.Number.Float"
    assert _kind_of(source, "V") == "Token.Keyword.Type"
    assert _kind_of(source, "0x1F") == "Token.Literal.Number.Hex"
    # A unit never runs into the next word.
    assert _kind_of("repeat 3 times { }\n", "times") == "Token.Keyword"


def test_text_bytes_and_comments() -> None:
    source = 'log "rtt {rtt * 1000:.3f} ms\\n"  # a note\nlet data = x"01 0a ff"\n'
    assert _kind_of(source, "{rtt * 1000:.3f}") == "Token.Literal.String.Interpol"
    assert _kind_of(source, "\\n") == "Token.Literal.String.Escape"
    assert _kind_of(source, "# a note") == "Token.Comment.Single"
    assert _kind_of(source, '"01 0a ff"') == "Token.Literal.String.Other"


def test_a_slash_is_a_regex_only_where_an_operand_goes() -> None:
    source = "check out matches /ready (\\d+)/i\nlet half = total / 2\n"
    assert _kind_of(source, "/ready (\\d+)/i") == "Token.Literal.String.Regex"
    assert _kind_of(source, "/") == "Token.Operator"
    call = "board.console.expect(/zed> /, within: 5 s)\n"
    assert _kind_of(call, "/zed> /") == "Token.Literal.String.Regex"


def test_text_outside_the_language_is_an_error() -> None:
    assert _errors("let x = 1;\n") == [";"]
    assert _errors('log "unclosed\nlog "fine"\n') == ["\n"]
    assert _errors("check x == 1\n") == []


SNIPPETS = documented_snippets()


def test_the_documentation_has_plan_snippets() -> None:
    assert len(SNIPPETS) >= 20


@pytest.mark.parametrize(
    ("where", "line", "source"),
    SNIPPETS,
    ids=[f"{where}:{line}" for where, line, _source in SNIPPETS],
)
def test_every_documented_snippet_lexes(where: str, line: int, source: str) -> None:
    errors = [
        (source.count("\n", 0, offset) + line, text)
        for offset, kind, text in JigLexer().get_tokens_unprocessed(source)
        if kind in Token.Error
    ]
    assert errors == [], f"{where}: error tokens (line, text): {errors}"
