"""Reading plan files: grammar, imports, string templates, source locations.

Imports::

    import "../lib/common.jig"       # relative to the importing file
    import "common.jig"              # beside the importing file, else in [paths] lib
    import "example-board:board.jig" # in the board's lib/ directory
    import "std:console.jig"         # Jig's plan library (jig/lang/stdlib)

An imported file holds constants and procedures only.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from textx import get_model, metamodel_from_str
from textx.exceptions import TextXError, TextXSyntaxError

from jig.errors import ConfigError, JigError
from jig.lang.values import UNITS

GRAMMAR: Traversable = resources.files("jig.lang").joinpath("grammar", "jig.tx")
SUFFIX = ".jig"
STDLIB = "std"  # the prefix of Jig's own plan library in imports
_UNITS_MARK = "@UNITS@"
_LIBRARY_REFERENCE = re.compile(r"(?P<prefix>[A-Za-z0-9][A-Za-z0-9._-]*):(?P<path>[^/\\].*)")


class PlanError(JigError):
    """A plan that does not parse, or that a statement cannot run as written."""

    def __init__(self, message: str, where: str = "") -> None:
        super().__init__(f"{where}: {message}" if where else message)
        self.where = where
        self.reason = message


def unit_alternation() -> str:
    """The grammar's unit alternation, built from ``values.UNITS``: longest
    first, so a unit is never cut short by a shorter one."""
    ordered = sorted(UNITS, key=lambda unit: (-len(unit), unit))
    return "|".join(re.escape(unit).replace("/", r"\/") for unit in ordered)


@functools.cache
def grammar_text() -> str:
    """The grammar with its unit alternation filled in."""
    text = GRAMMAR.read_text(encoding="utf-8")
    if _UNITS_MARK not in text:
        raise JigError(f"the grammar has no {_UNITS_MARK} placeholder")
    return text.replace(_UNITS_MARK, unit_alternation())


def _keywords() -> frozenset[str]:
    found = re.search(r"^Keyword:\s*/\((?P<words>[^)]*)\)", grammar_text(), re.MULTILINE)
    if found is None:
        raise JigError("the grammar has no Keyword rule")
    return frozenset(found.group("words").split("|"))


KEYWORDS = _keywords()


@functools.cache
def _metamodel() -> Any:
    return metamodel_from_str(grammar_text(), autokwd=True, memoization=True)


@functools.cache
def _expression_metamodel() -> Any:
    root = "TemplateExpr: expr=Expr;\n"
    return metamodel_from_str(root + grammar_text(), autokwd=True, memoization=True)


@dataclass
class Template:
    """A string literal with ``{expression}`` and ``{expression:format}`` fields."""

    parts: list[str | tuple[Any, str]]  # text, or (parsed expression, format spec)

    @property
    def constant(self) -> bool:
        return all(isinstance(part, str) for part in self.parts)


@dataclass
class Module:
    """One parsed plan or library file."""

    path: Path
    text: str
    model: Any
    imports: list[tuple[Module, str | None]] = field(default_factory=list)
    templates: dict[int, Template] = field(default_factory=dict)

    def items(self, kind: str) -> list[Any]:
        return [item for item in self.model.items if type(item).__name__ == kind]

    def location(self, node: Any) -> str:
        line, column = self.model._tx_parser.pos_to_linecol(node._tx_position)
        return f"{self.path.name}:{line}:{column}"

    def line(self, node: Any) -> int:
        return int(self.model._tx_parser.pos_to_linecol(node._tx_position)[0])

    def source(self, node: Any) -> str:
        text = self.text[node._tx_position : node._tx_position_end]
        return " ".join(text.split())


class Program:
    """A plan and every library it imports, with node-to-file lookups."""

    def __init__(self, main: Module) -> None:
        self.main = main
        self.modules: dict[str, Module] = {}
        self._collect(main)

    def _collect(self, module: Module) -> None:
        key = str(module.path)
        if key in self.modules:
            return
        self.modules[key] = module
        for child, _alias in module.imports:
            self._collect(child)

    def module_of(self, node: Any) -> Module:
        model = get_model(node)
        return self.modules[str(Path(model._tx_filename).resolve())]

    def location(self, node: Any) -> str:
        return self.module_of(node).location(node)

    def source(self, node: Any) -> str:
        return self.module_of(node).source(node)

    def template(self, node: Any) -> Template | None:
        """The compiled template of a string literal in a file (None for a string
        inside a template field)."""
        for module in self.modules.values():
            template = module.templates.get(id(node))
            if template is not None:
                return template
        return None


def load(path: Path) -> Program:
    """Parse ``path`` and its imports; raise PlanError with a location on errors."""
    return Program(_load(path.resolve(), ()))


def load_board_file(path: Path) -> Program:
    """Parse a board file: ``const`` lines, then one ``board "<name>" { ... }``.
    Anything else (a test, a system, an import) is a PlanError naming its
    line."""
    module = _parse(path.resolve(), "board file")
    definitions = module.items("BoardDefinition")
    for item in module.model.items:
        kind = type(item).__name__
        if kind in ("Const", "BoardDefinition"):
            if kind == "Const" and definitions and item._tx_position > definitions[0]._tx_position:
                raise PlanError(
                    "const lines come before the board definition", module.location(item)
                )
            continue
        raise PlanError(
            f"a board file holds const lines and one board definition, not {_article(kind)}",
            module.location(item),
        )
    if len(definitions) != 1:
        where = module.location(definitions[1]) if definitions else module.path.name
        raise PlanError("a board file holds one board definition", where)
    return Program(module)


_KIND_WORDS = {
    "Import": "an import",
    "System": "a system",
    "Procedure": "a procedure",
    "Setup": "a setup block",
    "Teardown": "a teardown block",
    "Test": "a test",
}


def _article(kind: str) -> str:
    return _KIND_WORDS.get(kind, kind.lower())


def _parse(path: Path, what: str) -> Module:
    if not path.is_file():
        raise PlanError(f"{what} {path} not found")
    text = path.read_text(encoding="utf-8")
    try:
        model = _metamodel().model_from_file(str(path))
    except TextXSyntaxError as exc:
        where = f"{path.name}:{exc.line}:{exc.col}"
        raise PlanError(_syntax_message(exc, text), where) from None
    except TextXError as exc:
        raise PlanError(str(exc), path.name) from None
    module = Module(path, text, model)
    _compile_templates(module)
    return module


def _load(path: Path, chain: tuple[Path, ...]) -> Module:
    if path in chain:
        cycle = " -> ".join(p.name for p in (*chain, path))
        raise PlanError(f"import cycle: {cycle}")
    module = _parse(path, "plan file")
    for item in module.items("BoardDefinition"):
        raise PlanError(
            "a plan file holds no board definition: a board is defined in boards/<name>/board.jig",
            module.location(item),
        )
    for item in module.items("Import"):
        target = resolve_import(unquote(item.path.value), path, module.location(item))
        child = _load(target, (*chain, path))
        for kind in ("System", "Setup", "Teardown", "Test"):
            if child.items(kind):
                raise PlanError(
                    f"{target.name} declares a {kind.lower()}, an imported file holds only "
                    "constants and procedures",
                    module.location(item),
                )
        module.imports.append((child, item.alias))
    return module


def stdlib_dir() -> Path:
    """Jig's plan library: ``import "std:<file>"``."""
    from jig import project

    return project.package_file("lang", "stdlib")


def _inside(base: Path, relative: str, reference: str, where: str) -> Path:
    base = base.resolve()
    target = (base / relative).resolve()
    if base != target and base not in target.parents:
        raise PlanError(f"import {reference!r} reaches outside {base}", where)
    return target


def resolve_import(reference: str, importer: Path, where: str = "") -> Path:
    """The file ``import "<reference>"`` in ``importer`` names (see the module
    documentation for the forms)."""
    from jig import project

    library = _LIBRARY_REFERENCE.fullmatch(reference)
    drive = re.match(r"[A-Za-z]:[\\/]", reference) is not None
    if library is not None and not drive:
        prefix, rest = library.group("prefix"), library.group("path")
        if prefix == STDLIB:
            base = stdlib_dir()
        else:
            from jig.system import boards

            try:
                base = boards.locate(prefix).directory / "lib"
            except ConfigError as exc:
                raise PlanError(f"import {reference!r}: {exc}", where) from None
        searched = [_inside(base, rest, reference, where)]
    elif reference.startswith(("./", "../", ".\\", "..\\")) or Path(reference).is_absolute():
        searched = [(importer.parent / reference).resolve()]
    else:
        searched = [(importer.parent / reference).resolve()]
        searched += [(directory / reference).resolve() for directory in project.current().lib]
    target = next((candidate for candidate in searched if candidate.is_file()), searched[0])
    if target.suffix != SUFFIX:
        raise PlanError(f"imports name {SUFFIX} files, not {target.name}", where)
    if not target.is_file():
        looked = ", ".join(str(candidate) for candidate in searched)
        raise PlanError(f"import {reference!r}: no such file (looked at {looked})", where)
    return target


_RENAMED = (
    (re.compile(r"\s*bus\s+\w+\s*:"), "a system declares a CAN bus as link NAME: can(...)"),
)


def _syntax_message(exc: TextXSyntaxError, text: str = "") -> str:
    lines = text.splitlines()
    if 0 < exc.line <= len(lines):
        for pattern, hint in _RENAMED:
            if pattern.match(lines[exc.line - 1]):
                return f"syntax error: {hint}"
    message = str(exc.message)
    if "Expected" in message:
        expected = message.split("Expected", 1)[1].split(" at position")[0]
        raw = [o.strip(" '") for o in expected.split(" or ")]
        # Token regexes (numbers, strings, names) read better as one word.
        options = [o for o in raw if "\\" not in o and "(" not in o and "[" not in o]
        if len(options) < len(raw):
            options.append("an expression")
        if len(options) > 8:
            options = [*options[:8], "..."]
        return "syntax error: expected " + ", ".join(options)
    return f"syntax error: {message}"


# ------------------------------------------------------------------ literals


_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "0": "\0", "{": "{{", "}": "}}"}


def unquote(literal: str) -> str:
    """A string literal's text with escapes processed (``\\n``, ``\\"``, ``\\xHH``)."""
    body = literal[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\" or index + 1 == len(body):
            out.append(char)
            index += 1
            continue
        code = body[index + 1]
        if code == "x" and index + 3 < len(body) + 1:
            out.append(chr(int(body[index + 2 : index + 4], 16)))
            index += 4
            continue
        out.append(_ESCAPES.get(code, "\\" + code))
        index += 2
    return "".join(out)


def _compile_templates(module: Module) -> None:
    from textx import get_children_of_type

    for node in get_children_of_type("Str", module.model):
        module.templates[id(node)] = _template(module, node)


def _template(module: Module, node: Any) -> Template:
    text = unquote(node.value)
    parts: list[str | tuple[Any, str]] = []
    buffer: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in "{}" and text[index : index + 2] in ("{{", "}}"):
            buffer.append(char)
            index += 2
            continue
        if char == "}":
            raise PlanError(
                "unmatched '}' in a string (write '}}' for a brace)", module.location(node)
            )
        if char != "{":
            buffer.append(char)
            index += 1
            continue
        end = _closing_brace(text, index)
        if end < 0:
            raise PlanError(
                "unclosed '{' in a string (write '{{' for a brace)", module.location(node)
            )
        expression, spec = _split_format(text[index + 1 : end])
        try:
            parsed = _expression_metamodel().model_from_str(expression).expr
        except TextXSyntaxError as exc:
            raise PlanError(
                f"in the string field {{{expression}}}: {_syntax_message(exc)}",
                module.location(node),
            ) from None
        if buffer:
            parts.append("".join(buffer))
            buffer = []
        parts.append((parsed, spec))
        index = end + 1
    if buffer or not parts:
        parts.append("".join(buffer))
    return Template(parts)


def _closing_brace(text: str, start: int) -> int:
    depth = 0
    quote = False
    for index in range(start, len(text)):
        char = text[index]
        if char == '"' and text[index - 1] != "\\":
            quote = not quote
        elif quote:
            continue
        elif char in "{([":
            depth += 1
        elif char in "})]":
            depth -= 1
            if depth == 0:
                return index if char == "}" else -1
    return -1


def _split_format(field_text: str) -> tuple[str, str]:
    """``value:.2f`` -> (``value``, ``.2f``): the last top-level colon starts the spec."""
    depth = 0
    quote = False
    split = -1
    for index, char in enumerate(field_text):
        if char == '"':
            quote = not quote
        elif quote:
            continue
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ":" and depth == 0:
            split = index
    if split < 0:
        return field_text, ""
    return field_text[:split], field_text[split + 1 :]
