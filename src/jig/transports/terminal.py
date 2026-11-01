"""The ``vt100`` layer: a console's output as a terminal shows it.

Firmware writes for a terminal: text, plus control characters and escape
sequences that move the cursor, edit the line and color the text. Line
editors such as funbiscuit's embedded-cli edit the input line with them as
keys arrive, so the bytes a board sends differ from the text a person reads.
:class:`Terminal` applies them the way a VT100 does, so a plan's patterns
match that text; :func:`encode_key` gives the bytes a terminal sends for a
named key. The bytes as sent are the caller's to keep: nothing here needs
them again.

Sources: DEC's VT100 User Guide, chapter 3 (control characters, the VT100's
control sequences and the codes its keys send), and xterm's "Control
Sequences" (ctlseqs) for what later terminals added (ICH, DCH, ECH, CHA,
``CSI s`` and ``CSI u``, and the keys a VT100 doesn't have).

What :class:`Terminal` renders:

* Bytes decode as latin-1, one character each, as a console without this
  layer decodes them. 0x80 to 0xFF print as characters, so UTF-8 text keeps
  its bytes; 8-bit controls such as 0x9B aren't read as CSI.
* Printable characters overwrite the cell at the cursor. CR goes to column
  0. LF, VT and FF start the next line at column 0, as with the VT100's new
  line mode (LNM) set, so a bare LF reads the same as CR LF. BS moves left
  without erasing. HT moves to the next tab stop; the stops are every 8
  columns, and the cells a tab skips read as spaces. BEL, NUL, DEL and the
  other control characters change nothing.
* Cursor moves (CUU, CUD, CUF, CUB, CHA, CUP, HVP), save and restore
  (DECSC, DECRC, ``CSI s``, ``CSI u``), insert, delete and erase characters
  (ICH, DCH, ECH), and erase in line and in display (EL and ED, 0 to 2).
  SGR, every other CSI sequence, other escape sequences and OSC, DCS, SOS,
  PM and APC strings are dropped. A sequence split across two feeds works.
  A malformed sequence loses its ESC, and the bytes after it are read as
  they stand.
* The screen has 24 rows, the VT100's. A line that scrolls off the top
  becomes history, which no sequence changes again: cursor-up and cursor
  addressing stop at the screen's top row, as a VT100's stop at its top
  margin.
* Lines don't wrap. A line grows as long as the firmware writes it; cursor
  moves to the right stop at column 79 (``columns`` - 1), or just past the
  end of a longer line, and nothing shifts off the end of a line.
* ED 2 (erase display) moves the screen's lines into history, as if they
  had scrolled off, and leaves a blank screen with the cursor where it was.
  What the firmware printed before it cleared the screen stays in
  :attr:`Terminal.text`. ED 3 and the other values are ignored.
* A line's trailing spaces aren't text, whether the firmware wrote them or
  an erase left them, except on the cursor's line, which runs at least to
  the cursor: a prompt's last space stays while the cursor sits after it.
  Rows below the cursor that hold no text aren't text either.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = ["COLUMNS", "KEYS", "ROWS", "Terminal", "encode_key"]

ROWS: Final = 24  # the VT100's screen height
COLUMNS: Final = 80  # where rightward cursor moves stop on a shorter line
TAB_WIDTH: Final = 8

# An escape sequence cut off by the end of a feed waits for the next feed, up
# to this many bytes; an unfinished one longer than this is malformed.
_MAX_PENDING: Final = 256
_BIG: Final = 999_999_999  # what a parameter of more than 9 digits reads as

_TOKENS: Final = re.compile(
    rb"([\x20-\x7e\x80-\xff]+)"  # 1: characters to print
    rb"|([\x00-\x1a\x1c-\x1f\x7f])"  # 2: a control character
    rb"|(\x1b\[([\x20-\x3f]*)([\x40-\x7e]))"  # 3: CSI; 4: parameters and intermediates; 5: final
    rb"|(\x1b[\x5dPX^_][^\x07\x1b]*(\x07|\x1b\\)?)"  # 6: OSC, DCS, SOS, PM, APC; 7: end
    rb"|(\x1b[78])"  # 8: DECSC, DECRC
    rb"|(\x1b(?:[\x20-\x2f]+[\x30-\x7e]|[\x30-\x5a\x5c-\x7e]))"  # 9: other escape sequences
    rb"|(\x1b)"  # 10: an ESC that starts no complete sequence
)
# What an unfinished sequence looks like at the end of a feed.
_PARTIAL: Final = re.compile(rb"\x1b(?:\[[\x20-\x3f]*|[\x20-\x2f]*)")
# A string (OSC and the like) ends at BEL, or at the ESC of ST (ESC \) or of
# whatever sequence comes next.
_STRING_END: Final = re.compile(rb"[\x07\x1b]")
_DIGITS: Final = b"0123456789;"


def _number(part: bytes) -> int:
    digits = part.lstrip(b"0")
    if not digits:
        return 0
    return int(digits) if len(digits) <= 9 else _BIG


def _count(params: list[bytes], index: int = 0) -> int:
    """A count or a position: absent or 0 means 1."""
    return max(1, _number(params[index])) if index < len(params) else 1


class Terminal:
    """A terminal's screen and history, fed the bytes a board sends.

    ``text`` is everything rendered since the start: the history, then the
    screen. ``mark()`` and ``since_mark`` serve a console's ``clear()`` and
    ``buffer``. The view a mark starts holds the cursor's line from the
    cursor's column on, and every line that output writes, erases or
    reaches with a line feed after the mark, in screen order. Each is
    shown as it now stands, and whole, except the mark's own line, which
    starts at the mark's column unless output changed it further left (a
    line editor recalling a command rewrites its line from column 0).
    Lines output doesn't touch stay out of the view, so a status line
    redrawn at the top of the screen doesn't bring the lines under it
    back. Before the first mark the view is all of ``text``.

    ``feed`` costs time in proportion to its input: a line is rendered
    once, when it leaves the screen.
    """

    def __init__(self, rows: int = ROWS, columns: int = COLUMNS) -> None:
        if rows < 1 or columns < 1:
            raise ValueError(f"a terminal needs at least 1 row and 1 column, not {rows}x{columns}")
        self.rows: Final = rows
        self.columns: Final = columns
        self._screen = [bytearray() for _ in range(rows)]
        self._y = 0
        self._x = 0
        self._saved = (0, 0)
        self._history = bytearray()  # lines that left the screen, each ended by LF
        self._pending = b""  # the start of a sequence the next feed finishes
        self._in_string = False  # inside an OSC (or similar) string the next feed ends
        self._marked = False
        # Per screen row, the column the since-mark view starts from on it
        # (None: the row isn't in the view), and the view's rows that have
        # left the screen.
        self._view: list[int | None] = [None] * rows
        self._view_history = bytearray()
        self._version = 0
        self._text_cache = (-1, "")
        self._since_cache = (-1, "")

    # ------------------------------------------------------------ views

    @property
    def text(self) -> str:
        """Everything rendered since the start: history, then the screen."""
        version, text = self._text_cache
        if version != self._version:
            text = (self._history + self._render()).decode("latin-1")
            self._text_cache = (self._version, text)
        return text

    @property
    def since_mark(self) -> str:
        """The view the last ``mark()`` started; all of ``text`` before one."""
        if not self._marked:
            return self.text
        version, text = self._since_cache
        if version != self._version:
            text = (self._view_history + self._render_view()).decode("latin-1")
            self._since_cache = (self._version, text)
        return text

    @property
    def line(self) -> str:
        """The cursor's line as it now stands."""
        return self._row_text(self._y).decode("latin-1")

    @property
    def cursor(self) -> tuple[int, int]:
        """The cursor's screen row (0 is the top) and column (0 is the left)."""
        return self._y, self._x

    def mark(self) -> None:
        """Start a new since-mark view at the cursor."""
        self._marked = True
        self._view = [None] * self.rows
        self._view[self._y] = self._x
        self._view_history = bytearray()
        self._version += 1

    # ------------------------------------------------------------ input

    def feed(self, data: bytes | bytearray) -> None:
        """Apply the bytes a board sent, in order."""
        if not data:
            return
        self._version += 1
        if self._pending:
            data = self._pending + data
            self._pending = b""
        start = 0
        if self._in_string:
            end = _STRING_END.search(data)
            if end is None:
                return  # all of it is inside the string
            self._in_string = False
            start = end.start()
            if data[start] == 0x07:
                start += 1  # BEL ends the string; an ESC begins what comes next
        for token in _TOKENS.finditer(data, start):
            kind = token.lastindex
            if kind == 1:
                self._print(token.group(1))
            elif kind == 2:
                self._control(data[token.start()])
            elif kind == 3:
                self._csi(token.group(4), data[token.end() - 1])
            elif kind == 6:
                if token.group(7) is None and token.end() == len(data):
                    self._in_string = True
            elif kind == 8:
                if data[token.start() + 1] == 0x37:  # ESC 7, DECSC
                    self._saved = (self._y, self._x)
                else:  # ESC 8, DECRC
                    self._y, self._x = self._saved
            elif kind == 10:
                rest = data[token.start() :]
                if len(rest) <= _MAX_PENDING and _PARTIAL.fullmatch(rest):
                    self._pending = bytes(rest)
                    return
            # 9, and 10 when malformed: dropped

    # ------------------------------------------------------------ actions

    def _print(self, run: bytes) -> None:
        y = self._y
        x = self._x
        row = self._screen[y]
        if x > len(row):
            row.extend(b" " * (x - len(row)))
        row[x : x + len(run)] = run
        self._x = x + len(run)
        start = self._view[y]
        if start is None:
            self._view[y] = 0
        elif x < start:
            self._view[y] = x

    def _changed(self, y: int, x: int) -> None:
        """Output changed screen row ``y`` from column ``x`` on."""
        start = self._view[y]
        if start is None:
            self._view[y] = 0
        elif x < start:
            self._view[y] = x

    def _control(self, code: int) -> None:
        # VT100 User Guide, chapter 3, the control characters it acts on.
        if code == 0x0D:  # CR
            self._x = 0
        elif code in (0x0A, 0x0B, 0x0C):  # LF, VT, FF: a new line (LNM set)
            self._x = 0
            if self._y + 1 < self.rows:
                self._y += 1
                if self._view[self._y] is None:
                    self._view[self._y] = 0
            else:
                self._scroll(joins_view=True)
        elif code == 0x08:  # BS: left, stopping at column 0
            if self._x:
                self._x -= 1
        elif code == 0x09:  # HT
            stop = (self._x // TAB_WIDTH + 1) * TAB_WIDTH
            self._x = max(self._x, min(stop, self._right()))

    def _csi(self, body: bytes, final: int) -> None:
        if final == 0x6D or body.strip(_DIGITS):
            return  # SGR, or a private or intermediate byte: nothing to render
        params = body.split(b";")
        # CUU, CUD, CUF, CUB, CUP, HVP, EL and ED: VT100 User Guide, chapter 3.
        # CHA, ICH, DCH, ECH, CSI s and CSI u: xterm ctlseqs, "Functions using CSI".
        if final == 0x41:  # CUU, ESC [ n A
            self._y = max(0, self._y - _count(params))
        elif final == 0x42:  # CUD, ESC [ n B
            self._y = min(self.rows - 1, self._y + _count(params))
        elif final == 0x43:  # CUF, ESC [ n C
            self._x = max(self._x, min(self._x + _count(params), self._right()))
        elif final == 0x44:  # CUB, ESC [ n D
            self._x = max(0, self._x - _count(params))
        elif final == 0x47:  # CHA, ESC [ n G
            self._x = min(_count(params) - 1, self._right())
        elif final in (0x48, 0x66):  # CUP, ESC [ r ; c H, and HVP, ESC [ r ; c f
            self._y = min(_count(params), self.rows) - 1
            self._x = min(_count(params, 1) - 1, self._right())
        elif final == 0x73 and not body:  # SCOSC, ESC [ s
            self._saved = (self._y, self._x)
        elif final == 0x75 and not body:  # SCORC, ESC [ u
            self._y, self._x = self._saved
        elif final == 0x40:  # ICH, ESC [ n @
            self._insert(_count(params))
        elif final == 0x50:  # DCH, ESC [ n P
            self._delete(_count(params))
        elif final == 0x58:  # ECH, ESC [ n X
            self._erase_characters(_count(params))
        elif final == 0x4B:  # EL, ESC [ n K
            self._erase_line(_number(params[0]))
        elif final == 0x4A:  # ED, ESC [ n J
            self._erase_display(_number(params[0]))

    def _right(self) -> int:
        """The furthest column a rightward move reaches on the cursor's row."""
        return max(self.columns - 1, len(self._screen[self._y]))

    def _insert(self, count: int) -> None:
        row = self._screen[self._y]
        if self._x < len(row):
            row[self._x : self._x] = b" " * min(count, self.columns)
            self._changed(self._y, self._x)

    def _delete(self, count: int) -> None:
        row = self._screen[self._y]
        if self._x < len(row):
            del row[self._x : self._x + count]
            self._changed(self._y, self._x)

    def _erase_characters(self, count: int) -> None:
        row = self._screen[self._y]
        x = self._x
        if x < len(row):
            end = min(x + count, len(row))
            row[x:end] = b" " * (end - x)
            self._changed(self._y, x)

    def _erase_line(self, which: int) -> None:
        row = self._screen[self._y]
        x = self._x
        if which == 0:  # from the cursor to the end
            if x < len(row):
                del row[x:]
                self._changed(self._y, x)
        elif which == 1:  # from the start through the cursor
            end = min(x + 1, len(row))
            if end:
                row[:end] = b" " * end
                self._changed(self._y, 0)
        elif which == 2 and row:  # the whole line
            row.clear()
            self._changed(self._y, 0)

    def _erase_display(self, which: int) -> None:
        if which == 0:  # from the cursor to the end of the screen
            self._erase_line(0)
            for y in range(self._y + 1, self.rows):
                if self._screen[y]:
                    self._screen[y].clear()
                    self._changed(y, 0)
        elif which == 1:  # from the start of the screen through the cursor
            for y in range(self._y):
                if self._screen[y]:
                    self._screen[y].clear()
                    self._changed(y, 0)
            self._erase_line(1)
        elif which == 2:  # the whole screen: its lines go to history
            for _ in range(self._last_text_row() + 1):
                self._scroll(joins_view=False)
            for row in self._screen:
                row.clear()
            self._view = [None] * self.rows

    def _scroll(self, joins_view: bool) -> None:
        """Move the top row into history and add a blank row at the bottom,
        which a line feed brings into the since-mark view."""
        line = self._screen.pop(0).rstrip(b" ")
        start = self._view.pop(0)
        if start is not None and self._marked:
            self._view_history += line[start:]
            self._view_history.append(0x0A)
        self._history += line
        self._history.append(0x0A)
        self._screen.append(bytearray())
        self._view.append(0 if joins_view else None)

    # ------------------------------------------------------------ rendering

    def _last_text_row(self) -> int:
        """The lowest screen row with anything but spaces on it, or -1."""
        for y in range(self.rows - 1, -1, -1):
            row = self._screen[y]
            if row and not row.isspace():
                return y
        return -1

    def _row_text(self, y: int) -> bytearray:
        row = self._screen[y]
        shown = row.rstrip(b" ")
        x = self._x
        if y != self._y or len(shown) >= x:
            return shown
        # The cursor's row runs to the cursor, the blanks before it included.
        return row[:x] if len(row) >= x else row + b" " * (x - len(row))

    def _render(self) -> bytes:
        """The screen's rows down to the last that is text, joined by LF."""
        last = max(self._y, self._last_text_row())
        return b"\n".join(self._row_text(y) for y in range(last + 1))

    def _render_view(self) -> bytes:
        """The screen's rows in the since-mark view, each from its start."""
        last = max(self._y, self._last_text_row())
        return b"\n".join(
            self._row_text(y)[start:]
            for y, start in enumerate(self._view[: last + 1])
            if start is not None
        )


# The bytes each named key sends. ESC [ is CSI.
_KEYS: dict[str, bytes] = {
    # VT100 User Guide, chapter 3: the cursor keys with cursor key mode
    # (DECCKM) reset. Set, they send ESC O A to ESC O D, which embedded-cli
    # would take for the letters O and A typed: a sequence starts at ESC [.
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    # The VT100 has none of these six keys. xterm ctlseqs, "PC-Style Function
    # Keys", with DECCKM reset. In the places of Home and End, the VT220's
    # editing keypad has Find and Select, which send ESC [ 1 ~ and ESC [ 4 ~
    # (ctlseqs, "VT220-Style Function Keys").
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "page_up": b"\x1b[5~",
    "page_down": b"\x1b[6~",
    "tab": b"\t",
    # The VT100's RETURN key sends CR, or CR LF with new line mode (LNM) set
    # (chapter 3). Jig sends CR: a CLI that ends a command at either CR or LF
    # would read CR LF as a command and an empty line.
    "enter": b"\r",
    # The VT100's BACKSPACE key sends BS (0x08) and its DELETE key DEL (0x7F)
    # (chapter 3); xterm's backarrow key sends BS or DEL as DECBKM is set or
    # reset (ctlseqs, CSI ? 67 h and CSI ? 67 l). Jig sends DEL, the erase
    # character of a Unix terminal's default settings (CERASE, 0177, in
    # <sys/ttydefaults.h>); embedded-cli and linenoise read both as backspace.
    "backspace": b"\x7f",
    "escape": b"\x1b",
}
# CTRL with a letter sends that letter's control code, 0x01 for A to 0x1A for
# Z (VT100 User Guide, chapter 3).
_KEYS.update({f"ctrl-{chr(code)}": bytes([code - 0x60]) for code in range(0x61, 0x7B)})

KEYS: Final[Mapping[str, bytes]] = MappingProxyType(_KEYS)
"""The named keys and the bytes each one sends."""


def encode_key(name: str) -> bytes:
    """The bytes a terminal sends for the named key: ``up``, ``down``,
    ``right``, ``left``, ``home``, ``end``, ``insert``, ``delete``,
    ``page_up``, ``page_down``, ``tab``, ``enter``, ``backspace``,
    ``escape``, or ``ctrl-a`` to ``ctrl-z``."""
    try:
        return KEYS[name]
    except KeyError:
        names = ", ".join(key for key in KEYS if not key.startswith("ctrl-"))
        raise ValueError(f"no key named {name!r}; the keys are {names}, ctrl-a to ctrl-z") from None
