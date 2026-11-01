"""Pygments lexer for Jig plans and board files (``.jig`` files).

jig-dsl registers :class:`JigLexer` under the ``pygments.lexers`` entry point
as ``jig``, so Sphinx, ``pygmentize -l jig`` and other Pygments users highlight
plans once the package is installed. The rules follow the grammar in
``jig/lang/grammar/jig.tx``:

* the reserved words of statements, word operators (``over`` among them) and
  ``true false none``;
* declarations (``system``, ``board``, ``link``, ``node``, ``const``,
  ``procedure``, ``setup``, ``teardown``, ``test``, ``import``, and in board
  files ``option``, ``device``, ``peripheral``, ``external link``,
  ``sensor``, ``boot``, ``protocol``, ``interface``, ``ip``, ``unmodelled``)
  only where they open a declaration, since the grammar lets ``board`` or
  ``test`` name a variable elsewhere.
* quantities, a number and its unit (``100 ms``, ``1 Mbit/s``).
* text with escapes and ``{field}`` interpolation, and bytes (``x"01 ff"``).
* regular expressions (``/ready (\\d+)/i``) where an operand is expected, and
  division where an operator is.
* ``#`` comments.

Pygments marks anything else as an error token, which the documentation test
uses to catch snippets that are not valid Jig.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pygments.lexer import (  # type: ignore[import-untyped,unused-ignore]
    RegexLexer,
    bygroups,
    default,
    include,
    words,
)
from pygments.token import (  # type: ignore[import-untyped,unused-ignore]
    Comment,
    Error,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Whitespace,
)

__all__ = ["JigLexer"]

# The grammar's reserved statement words (Keyword in jig.tx), less the word
# operators and constants, which get their own token types.
KEYWORDS = (
    "let",
    "if",
    "elif",
    "else",
    "repeat",
    "times",
    "every",
    "for",
    "while",
    "parallel",
    "race",
    "task",
    "timeline",
    "at",
    "try",
    "catch",
    "check",
    "wait",
    "until",
    "within",
    "log",
    "record",
    "fail",
    "return",
    "break",
    "continue",
    "as",
)
WORD_OPERATORS = ("and", "or", "not", "in", "contains", "matches", "over")
CONSTANTS = ("true", "false", "none")

# Functions every plan sees (jig.lang.library.builtin_functions), the
# constructors a system declaration uses, the protocol layers and what board
# files declare with.
BUILTINS = (
    "now",
    "len",
    "abs",
    "min",
    "max",
    "round",
    "floor",
    "ceil",
    "sum",
    "range",
    "text",
    "number",
    "match",
    "regex",
    "hex",
    "from_hex",
    "read_file",
    "random_bytes",
    "crc32",
    "crc32c",
    "during",
    "off_profile",
    "constant",
    "sine",
    "ramp",
    "table",
    "steps",
    "time_after",
    "seconds_between",
    "date_fields",
    "uart",
    "can",
    "spi",
    "qspi",
    "i2c",
    "gpio",
    "csp",
    "kiss",
    "cfp",
    "cli",
    "vt100",
    "choice",
    "memory",
    "cosim",
    "airhdl",
    "build",
    "command",
    "zynq7000",
    "stm32",
)

# Words that open a declaration followed by a name and a colon.
DECLARATIONS = (
    "board",
    "link",
    "node",
    "option",
    "device",
    "peripheral",
    "sensor",
    "protocol",
    "interface",
    "ip",
    "unmodelled",
)

# The grammar's Quantity units, longest alternatives first where one is a
# prefix of another, a unit never runs into a following letter or digit.
UNIT = (
    r"(?:ns|us|ms|min|mV|mA|uA|h|s|V|A|GHz|MHz|kHz|Hz|KiB|MiB|B"
    r"|Mbit/s|kbit/s|bit/s)(?![A-Za-z0-9_])"
)

IDENTIFIER = r"[^\W\d]\w*"


def _quantity(number: str, token: Any) -> tuple[str, Any]:
    """A number literal of one kind, then optional blanks and a unit."""
    return rf"({number})([ \t]*)({UNIT})?", bygroups(token, Whitespace, Keyword.Type)


class JigLexer(RegexLexer):  # type: ignore[misc,unused-ignore]
    """Lexer for Jig test plans."""

    name = "Jig"
    url = "https://jig.readthedocs.io"
    aliases = ("jig",)
    filenames = ("*.jig",)
    mimetypes = ("text/x-jig",)

    tokens: ClassVar[dict[str, list[Any]]] = {
        "blank": [
            (r"\s+", Whitespace),
            (r"#.*?$", Comment.Single),
        ],
        # Where an operand comes next, a slash opens a regular expression.
        "operand": [
            include("blank"),
            (r"/(?:\\.|[^/\\\n])+/[imsx]*", String.Regex, "#pop"),
            default("#pop"),
        ],
        "string": [
            (r'"', String.Double, "#pop"),
            # Text ends on its line; an unclosed one is an error, not a reset.
            (r"\n", Error, "#pop"),
            (r"\\x[0-9A-Fa-f]{2}", String.Escape),
            (r"\\.", String.Escape),
            (r"\{\{|\}\}", String.Escape),
            (r'\{[^{}"\n]*\}', String.Interpol),
            (r'[^"\\{}\n]+', String.Double),
            (r"[{}]", String.Double),
        ],
        "root": [
            include("blank"),
            # Declarations: these words are reserved only where they open one.
            (r"(import)(\s+)(?=\")", bygroups(Keyword.Namespace, Whitespace)),
            (
                rf"(external)(\s+)(link)(\s+)({IDENTIFIER})(\s*)(:)",
                bygroups(
                    Keyword.Declaration,
                    Whitespace,
                    Keyword.Declaration,
                    Whitespace,
                    Name.Variable,
                    Whitespace,
                    Punctuation,
                ),
                "operand",
            ),
            (
                rf"({'|'.join(DECLARATIONS)})(\s+)({IDENTIFIER})(\s*)(:)",
                bygroups(Keyword.Declaration, Whitespace, Name.Variable, Whitespace, Punctuation),
                "operand",
            ),
            # boot jtag, boot qspi: {...}; protocol csp(...) without a name
            (
                rf"(boot)(\s+)({IDENTIFIER})",
                bygroups(Keyword.Declaration, Whitespace, Name.Variable),
            ),
            (r"(protocol)(?=\s+[^\W\d]\w*\s*\()", Keyword.Declaration),
            (r"(board)(?=\s*\")", Keyword.Declaration),
            (
                rf"(const)(\s+)({IDENTIFIER})(\s*)(=)",
                bygroups(Keyword.Declaration, Whitespace, Name.Constant, Whitespace, Operator),
                "operand",
            ),
            (
                rf"(procedure)(\s+)({IDENTIFIER})",
                bygroups(Keyword.Declaration, Whitespace, Name.Function),
            ),
            (r"(system|test)(?=\s*[\"{])", Keyword.Declaration),
            (r"(setup|teardown)(?=\s*\{)", Keyword.Declaration),
            (words(CONSTANTS, suffix=r"\b"), Keyword.Constant),
            (words(WORD_OPERATORS, suffix=r"\b"), Operator.Word, "operand"),
            (words(KEYWORDS, suffix=r"\b"), Keyword, "operand"),
            # Bytes before names: x"01 0a ff".
            (r'(x)("[0-9A-Fa-f\s_]*")', bygroups(String.Affix, String.Other)),
            (r'"', String.Double, "string"),
            _quantity(r"0x[0-9A-Fa-f_]+", Number.Hex),
            _quantity(r"0b[01_]+", Number.Bin),
            _quantity(r"\d[\d_]*(?:\.\d+(?:[eE][+-]?\d+)?|[eE][+-]?\d+)", Number.Float),
            _quantity(r"\d[\d_]*", Number.Integer),
            (words(BUILTINS, suffix=r"\b(?=\s*\()"), Name.Builtin),
            (IDENTIFIER, Name),
            (r"==|!=|<=|>=|\+=|-=|\.\.|[<>=+\-*/%]", Operator, "operand"),
            (r"[(\[{,:]", Punctuation, "operand"),
            (r"[)\]}.]", Punctuation),
        ],
    }
