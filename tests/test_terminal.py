"""The vt100 layer: what a terminal shows for the bytes a board sends.

The line-editing streams are the bytes funbiscuit's embedded-cli (MIT,
github.com/funbiscuit/embedded-cli) writes: it echoes a key typed at the end
of the line as it is, inserts with ESC [ @ and the character, deletes with
ESC [ D and ESC [ P, moves with ESC [ C and ESC [ D, and clears its line for
a recalled command or a message by writing CR, a space over every cell of
the line, and CR again.
"""

from __future__ import annotations

import re
import time

import pytest

from jig.transports.terminal import KEYS, Terminal, encode_key

PROMPT = b"\x1b[1;32mjig\x1b[0m> "  # "jig> " in bold green

# A session that uses most of what the terminal renders, for the split tests.
SESSION = (
    b"boot\r\n"
    + PROMPT
    + b"h\x1b[selp\x1b[ue\x1b[slp\x1b[ulp\r\n * help\r\n"
    + PROMPT
    + b"stauts\x1b[D\x1b[D\x1b[D\x1b[P\x1b[C\x1b[@u\x1b[3C\r\nok\r\n"
    + PROMPT
    + b"\r"
    + b" " * 5
    + b"\r"
    + PROMPT
    + b"status\x1b]0;title\x07\x1b[2K\r"
    + b"".join(b"line %d\x1b[K\r\n" % n for n in range(30))
    + b"\x1b[5A\x1b7\x1b[1;1Htop\x1b8\x1b[3X\x1b[2J\x1b[Hdone"
)


def rendered(*chunks: bytes, rows: int = 24) -> Terminal:
    terminal = Terminal(rows=rows)
    for chunk in chunks:
        terminal.feed(chunk)
    return terminal


# ---------------------------------------------------------------- control characters


def test_lines_and_the_cursor_line() -> None:
    terminal = rendered(b"abc\r\ndef")
    assert terminal.text == "abc\ndef"
    assert terminal.line == "def"
    assert terminal.cursor == (1, 3)


def test_a_bare_line_feed_starts_the_next_line_at_column_0() -> None:
    assert rendered(b"abc\ndef").text == "abc\ndef"
    assert rendered(b"abc\n\rdef").text == "abc\ndef"
    assert rendered(b"a\x0bb\x0cc").text == "a\nb\nc"  # VT and FF act as LF


def test_carriage_return_overwrites_the_line() -> None:
    assert rendered(b"12345\rab").text == "ab345"


def test_backspace_moves_left_without_erasing_and_stops_at_column_0() -> None:
    assert rendered(b"abc\b\bX").text == "aXc"
    assert rendered(b"ab\b\b\b\bX").text == "Xb"


def test_tab_moves_to_the_next_stop_of_eight() -> None:
    assert rendered(b"a\tb").text == "a" + " " * 7 + "b"
    terminal = rendered(b"12345678\tx")
    assert terminal.text == "12345678" + " " * 8 + "x"
    terminal = rendered(b"\x1b[79G\tx")  # no stop left on the line: HT goes to the last column
    assert terminal.line == " " * 79 + "x"


def test_bell_nul_and_del_change_nothing() -> None:
    assert rendered(b"a\x07b\x00c\x7fd\x0e\x0fe").text == "abcde"


def test_bytes_from_0x80_print_as_latin_1() -> None:
    assert rendered(b"23 \xc2\xb0C").text == "23 \xc2\xb0C"  # a degree sign's two UTF-8 bytes
    assert rendered(b"a\x9b2Jb").text == "a\x9b2Jb"  # 8-bit CSI isn't read as one


# ---------------------------------------------------------------- sequences


def test_cursor_moves_and_their_defaults() -> None:
    terminal = rendered(b"abcdefghij", b"\x1b[D\x1b[0D\x1b[3D", b"X")
    assert terminal.line == "abcdeXghij"
    terminal.feed(b"\x1b[C\x1b[2CY")
    assert terminal.line == "abcdeXghiY"

    terminal = rendered(b"1\r\n2\r\n3", rows=5)
    terminal.feed(b"\x1b[A\x1b[0A\x1b[5Aa")  # up 1, up 1, then stops at the top row
    assert terminal.cursor == (0, 2)
    terminal.feed(b"\x1b[Bb")
    terminal.feed(b"\x1b[30Bz")  # down stops at the bottom row
    assert terminal.text == "1a\n2 b\n3\n\n   z"
    assert terminal.cursor == (4, 4)


def test_cursor_position_and_column() -> None:
    terminal = rendered(b"\x1b[3;4HX\x1b[HY\x1b[;2fZ\x1b[9;1HW\x1b[2GV\x1b[0;0HU", rows=5)
    assert terminal.text == "UZ\n\n   X\n\nWV"
    assert terminal.cursor == (0, 1)
    terminal.feed(b"\x1b[200G")  # stops at the last column
    assert terminal.cursor == (0, 79)


def test_save_and_restore_in_both_forms() -> None:
    terminal = rendered(b"abc\x1b7\r\ndef\x1b8X\x1b[s\x1b[2;1HY\x1b[uZ")
    assert terminal.text == "abcXZ\nYef"
    assert rendered(b"abc\x1b8X").text == "Xbc"  # nothing saved: home


def test_csi_s_and_csi_u_with_parameters_are_other_sequences() -> None:
    terminal = rendered(b"abc\x1b[1;5sX\x1b[1uY")  # DECSLRM and a key report
    assert terminal.text == "abcXY"
    terminal.feed(b"\x1b[uZ")
    assert terminal.text == "ZbcXY"


def test_insert_characters() -> None:
    terminal = rendered(b"abcdef\x1b[4D\x1b[@")
    assert terminal.line == "ab cdef"
    terminal.feed(b"\x1b[2@")
    assert terminal.line == "ab   cdef"
    assert terminal.cursor == (0, 2)
    terminal.feed(b"\x1b[0@")
    assert terminal.line == "ab    cdef"
    assert rendered(b"abc\x1b[5@").line == "abc"  # past the end: nothing to move


def test_delete_characters() -> None:
    terminal = rendered(b"abcdef\x1b[5D\x1b[P")
    assert terminal.line == "acdef"
    terminal.feed(b"\x1b[2P")
    assert terminal.line == "aef"
    terminal.feed(b"\x1b[99P")
    assert terminal.line == "a"
    terminal.feed(b"\x1b[P")
    assert terminal.line == "a"


def test_erase_characters() -> None:
    terminal = rendered(b"abcdef\x1b[5D\x1b[2X")
    assert terminal.line == "a  def"
    assert terminal.cursor == (0, 1)
    terminal.feed(b"\x1b[99X")
    assert terminal.line == "a"
    assert rendered(b"abc\x1b[5X").line == "abc"


def test_erase_in_line() -> None:
    assert rendered(b"abcdef\x1b[3D\x1b[K\r\n").text == "abc\n"
    assert rendered(b"abcdef\x1b[3D\x1b[0K\r\n").text == "abc\n"
    assert rendered(b"abcdef\x1b[3D\x1b[1K\r\n").text == "    ef\n"
    assert rendered(b"abcdef\x1b[3D\x1b[2K\r\n").text == "\n"
    terminal = rendered(b"abcdef\x1b[3D\x1b[2K")
    assert terminal.line == "   "  # the cursor's line runs to the cursor
    assert rendered(b"\x1b[1K\x1b[2K").text == ""


def test_erase_in_display_below_and_above() -> None:
    lines = b"aaa\r\nbbb\r\nccc\r\nddd"
    assert rendered(lines, b"\x1b[2;2H\x1b[J", rows=4).text == "aaa\nb"
    assert rendered(lines, b"\x1b[3;2H\x1b[1J", rows=4).text == "\n\n  c\nddd"
    assert rendered(b"aaa\r\n\r\nccc\x1b[1;2H\x1b[J", rows=4).text == "a"
    assert rendered(b"\r\nbbb\r\nccc\x1b[1J", rows=4).text == "\n\n   "


def test_erase_display_keeps_the_old_screen_in_history() -> None:
    terminal = rendered(b"one\r\ntwo\r\n\x1b[H\x1b[2J")
    assert terminal.text == "one\ntwo\n"
    terminal.feed(b"menu")
    assert terminal.text == "one\ntwo\nmenu"

    terminal = rendered(b"one\r\ntwo\r\n\x1b[2J")  # the cursor stays on row 2
    assert terminal.text == "one\ntwo\n\n\n"
    terminal.feed(b"\x1b[Hmenu")
    assert terminal.text == "one\ntwo\nmenu"
    terminal.feed(b"\x1b[5A\r!")  # the old screen is history now
    assert terminal.text == "one\ntwo\n!enu"

    assert rendered(b"abc\x1b[3J").text == "abc"


def test_colors_and_other_sequences_are_dropped() -> None:
    stream = (
        b"a\x1b[31mb\x1b[0m"  # SGR
        b"c\x1b[?25ld\x1b[?25h"  # cursor off and on (private)
        b"e\x1b[2 qf"  # cursor style (intermediate byte)
        b"\x1b(Bg\x1bch"  # character set, reset
        b"\x1b]0;title\x07i\x1b]2;t\x1b\\j"  # window titles, ended by BEL and by ST
        b"\x1bP1$r\x1b\\k"  # DCS
        b"\x1b[>4;2ml\x1b[5n"  # key modifiers, a status report request
    )
    assert rendered(stream).text == "abcdefghijkl"


def test_a_malformed_sequence_loses_its_escape() -> None:
    assert rendered(b"a\x1b\x01b").text == "ab"
    assert rendered(b"a\x1b[1\x80b").text == "a[1\x80b"


def test_large_parameters_are_clamped() -> None:
    terminal = rendered(b"\x1b[99999999999999999999Cx")
    assert terminal.line == " " * 79 + "x"
    terminal = rendered(b"abc\x1b[3D\x1b[99999@")  # at most a screen width of blanks
    assert terminal.line == " " * 80 + "abc"
    terminal = rendered(b"abc\x1b[" + b"9" * 5000 + b"D" + b"\x1b[" + b"0" * 5000 + b"1C!")
    assert terminal.line == "a!c"


# ---------------------------------------------------------------- feeds


def test_a_sequence_split_across_feeds() -> None:
    stream = b"ab\x1b[12;1H\x1b[2Ac"
    whole = rendered(stream)
    for cut in range(1, len(stream)):
        split = rendered(stream[:cut], stream[cut:])
        assert (split.text, split.cursor) == (whole.text, whole.cursor), cut
    assert rendered(b"x\x1b]0;tit", b"le\x07y").text == "xy"
    assert rendered(b"x\x1b]0;t\x1b", b"\\y").text == "xy"
    assert rendered(b"x\x1b", b"[1;31my").text == "xy"
    assert rendered(b"x\x1b", b"7y\x1b8z").text == "xz"


def test_any_split_renders_the_same() -> None:
    stream = SESSION
    whole = rendered(stream)
    middle = len(stream) // 2
    for size in (1, 2, 3, 5, 7, 64):
        chunks = [stream[i : i + size] for i in range(0, len(stream), size)]
        terminal = rendered(*chunks)
        assert (terminal.text, terminal.cursor) == (whole.text, whole.cursor), size

        marked = Terminal()
        for i in range(0, middle, size):
            marked.feed(stream[i : min(i + size, middle)])
        marked.mark()
        for i in range(middle, len(stream), size):
            marked.feed(stream[i : i + size])
        reference = rendered(stream[:middle])
        reference.mark()
        reference.feed(stream[middle:])
        assert marked.since_mark == reference.since_mark, size


def test_a_string_left_open_swallows_feeds_until_it_ends() -> None:
    terminal = rendered(b"a\x1b]0;", b"title", b"more\r\n", b"\x07b")
    assert terminal.text == "ab"
    assert rendered(b"a\x1b]0;", b"title\x1b\\b").text == "ab"  # ended by ST
    assert rendered(b"a\x1b]0;", b"t\x1b[1;31mb").text == "ab"  # cut short by the next ESC


def test_feeding_nothing_changes_nothing() -> None:
    terminal = rendered(b"abc")
    text = terminal.text
    terminal.feed(b"")
    assert terminal.text is text


def test_an_unfinished_sequence_waits_only_so_long() -> None:
    long = b"\x1b[" + b"1;" * 200
    assert rendered(long).text == "[" + "1;" * 200  # too long to wait: malformed
    half = b"\x1b[" + b"1;" * 100
    assert rendered(half, b"1;" * 100 + b"m").text == ""


def test_feed_takes_a_bytearray() -> None:
    terminal = Terminal()
    terminal.feed(bytearray(b"ab\x1b["))
    terminal.feed(bytearray(b"D!"))
    assert terminal.text == "a!"


# ---------------------------------------------------------------- the screen


def test_cursor_up_stops_at_the_top_of_the_screen() -> None:
    lines = [f"line {n}" for n in range(30)]
    terminal = rendered("".join(f"{line}\r\n" for line in lines).encode())
    terminal.feed(b"\x1b[99A")
    assert terminal.cursor == (0, 0)
    assert terminal.line == "line 7"  # lines 0 to 6 have scrolled into history
    terminal.feed(b"X\x1b[1;1HY")
    assert terminal.text.split("\n") == [*lines[:7], "Yine 7", *lines[8:]]


def test_lines_do_not_wrap() -> None:
    terminal = rendered(b"x" * 200)
    assert terminal.text == "x" * 200
    assert terminal.cursor == (0, 200)
    terminal.feed(b"\r\nab\x1b[999Cc")  # a rightward move stops at the last column
    assert terminal.line == "ab" + " " * 77 + "c"
    terminal.feed(b"\r\n" + b"y" * 100 + b"\x1b[20D\x1b[999Cz")  # or at a longer line's end
    assert terminal.line == "y" * 100 + "z"


def test_trailing_spaces_are_not_text_except_up_to_the_cursor() -> None:
    terminal = rendered(b"> ")
    assert terminal.text == "> "
    terminal.feed(b"\r\nabc\b \b")  # the usual erase: back, space, back
    assert terminal.line == "ab"
    terminal.feed(b"\r\n")
    assert terminal.text == ">\nab\n"


def test_rows_below_the_cursor_without_text_are_not_text() -> None:
    terminal = rendered(b"a\r\nb\r\nc\x1b[2A")
    assert terminal.text == "a\nb\nc"
    terminal.feed(b"\x1b[2B\x1b[2K\x1b[2A")
    assert terminal.text == "a\nb"


def test_a_terminal_needs_a_row_and_a_column() -> None:
    with pytest.raises(ValueError, match="at least 1 row"):
        Terminal(rows=0)
    with pytest.raises(ValueError, match="at least 1 row"):
        Terminal(columns=0)


def test_one_row() -> None:
    terminal = rendered(b"a\r\nb\r\nc\x1b[Ad", rows=1)
    assert terminal.text == "a\nb\ncd"


# ---------------------------------------------------------------- line editing


def test_typing_and_echo() -> None:
    terminal = rendered(PROMPT)
    for key in b"help":  # each key echoed as it is typed
        terminal.feed(bytes([key]))
    assert terminal.line == "jig> help"
    terminal.feed(b"\r\n * help\r\n\tlist the commands\r\n" + PROMPT)
    assert terminal.text == "jig> help\n * help\n" + " " * 8 + "list the commands\njig> "


def test_inserting_in_the_middle_of_a_line() -> None:
    terminal = rendered(PROMPT, b"hllo", b"\x1b[D" * 3)
    assert terminal.cursor == (0, 6)
    terminal.feed(b"\x1b[@e")  # make room at the cursor, then the key
    assert terminal.line == "jig> hello"
    assert terminal.cursor == (0, 7)


def test_deleting() -> None:
    terminal = rendered(PROMPT, b"hello")
    terminal.feed(b"\x1b[D\x1b[P")  # backspace at the end of the line
    assert terminal.line == "jig> hell"
    terminal.feed(b"\x1b[D\x1b[D" + b"\x1b[D\x1b[P")  # two lefts, then backspace
    assert terminal.line == "jig> hll"
    assert terminal.cursor == (0, 6)


def test_history_recall_rewrites_the_line() -> None:
    terminal = rendered(PROMPT, b"status\r\nok\r\n", PROMPT)
    terminal.mark()
    terminal.feed(b"\r" + b" " * 5 + b"\r" + PROMPT + b"status")  # up: the last command
    assert terminal.text == "jig> status\nok\njig> status"
    assert terminal.since_mark == "jig> status"  # rewritten from column 0: all of it
    terminal.feed(b"\r" + b" " * 11 + b"\r" + PROMPT + b"id")  # up: an older, shorter one
    assert terminal.line == "jig> id"
    assert terminal.since_mark == "jig> id"


def test_live_autocompletion_drawn_after_the_cursor() -> None:
    terminal = rendered(PROMPT, b"h\x1b[selp\x1b[u", b"e\x1b[slp\x1b[u")
    assert terminal.line == "jig> help"  # the completion shows past the cursor
    assert terminal.cursor == (0, 7)
    terminal.feed(b"x\x1b[s  \x1b[u")  # no command starts "hex": spaces over the rest
    assert terminal.line == "jig> hex"


def test_a_prompt_in_color() -> None:
    terminal = rendered(PROMPT)
    assert terminal.text == "jig> "
    assert terminal.cursor == (0, 5)


def test_a_message_printed_while_a_command_is_typed() -> None:
    terminal = rendered(PROMPT, b"sta")
    terminal.mark()
    terminal.feed(b"\r" + b" " * 8 + b"\r" + b"alarm: low battery\r\n" + PROMPT + b"sta")
    assert terminal.text == "alarm: low battery\njig> sta"
    assert terminal.since_mark == "alarm: low battery\njig> sta"


# ---------------------------------------------------------------- patterns


def test_a_pattern_for_the_visible_text_matches_after_rendering() -> None:
    stream = PROMPT + b"hllo" + b"\x1b[D" * 3 + b"\x1b[@e"
    terminal = rendered(stream)
    assert re.search(r"jig> hello$", terminal.text)
    assert not re.search(r"jig> hello", stream.decode("latin-1"))


def test_a_pattern_written_across_an_escape_sequence_does_not_match() -> None:
    stream = PROMPT + b"ready"
    raw = r"jig\x1b\[0m> ready"
    assert re.search(raw, stream.decode("latin-1"))
    assert not re.search(raw, rendered(stream).text)
    assert re.search(r"jig> ready", rendered(stream).text)


# ---------------------------------------------------------------- since the mark


def test_since_mark_starts_at_the_cursor() -> None:
    terminal = rendered(b"boot\r\n", PROMPT)
    terminal.mark()
    assert terminal.since_mark == ""
    terminal.feed(b"help\r\nok\r\n" + PROMPT)
    assert terminal.since_mark == "help\nok\njig> "
    assert terminal.text == "boot\njig> help\nok\njig> "


def test_before_any_mark_the_view_is_all_the_text() -> None:
    terminal = rendered(b"a\r\n\x1b[4;1Hb")
    assert terminal.text == "a\n\n\nb"
    assert terminal.since_mark == terminal.text


def test_a_line_redrawn_from_its_start_is_in_the_view_whole() -> None:
    terminal = rendered(b"flashing [##    ] 30%")
    terminal.mark()
    terminal.feed(b"\rflashing [####  ] 60%")
    assert terminal.since_mark == "flashing [####  ] 60%"


def test_lines_output_does_not_touch_stay_out_of_the_view() -> None:
    terminal = rendered(b"status 1\r\nmenu a\r\nmenu b\r\n", PROMPT, rows=6)
    terminal.mark()
    terminal.feed(b"\x1b[s\x1b[1;1Hstatus 2\x1b[u")  # a status line redrawn at the top
    assert terminal.since_mark == "status 2\n"
    terminal.feed(b"x")
    assert terminal.since_mark == "status 2\nx"

    terminal = rendered(b"a\r\nb\r\nc\x1b[2A")  # lines below the cursor
    terminal.mark()
    terminal.feed(b"Z")
    assert terminal.since_mark == "Z"


def test_edits_bring_their_lines_into_the_view() -> None:
    terminal = rendered(b"one\r\ntwo\r\nthree\r\n", PROMPT)
    terminal.mark()
    terminal.feed(b"\x1b[3;1H\x1b[P")  # delete a character of "three"
    terminal.feed(b"\x1b[1;1H\x1b[K")  # erase "one"
    terminal.feed(b"\x1b[4;6H")  # back to the prompt
    assert terminal.since_mark == "\nhree\n"

    terminal = rendered(PROMPT)
    terminal.mark()
    terminal.feed(b"\x1b[3D\x1b[P")  # an edit left of the mark: the view starts there
    assert terminal.text == "ji>"  # the space after ">" is past the cursor now
    assert terminal.since_mark == ">"


def test_a_line_feed_onto_a_line_already_in_the_view() -> None:
    terminal = rendered(PROMPT)
    terminal.mark()
    terminal.feed(b"\r\nab\x1b[A\r\nc")
    assert terminal.since_mark == "\ncb"


def test_the_view_keeps_lines_that_scrolled_away() -> None:
    terminal = rendered(PROMPT)
    terminal.mark()
    output = [f"out {n}" for n in range(30)]
    terminal.feed(b"cmd\r\n" + "".join(f"{line}\r\n" for line in output).encode() + PROMPT)
    assert terminal.since_mark == "\n".join(["cmd", *output, "jig> "])
    assert terminal.text == "\n".join(["jig> cmd", *output, "jig> "])


def test_the_view_keeps_blank_lines() -> None:
    terminal = rendered(PROMPT)
    terminal.mark()
    terminal.feed(b"\r\n\r\nx")
    assert terminal.since_mark == "\n\nx"
    terminal = rendered(b"\r\n" * 40, PROMPT)  # the same at the bottom of the screen
    terminal.mark()
    terminal.feed(b"\r\n\r\nx")
    assert terminal.since_mark == "\n\nx"


def test_the_view_after_the_screen_is_erased() -> None:
    terminal = rendered(b"old\r\n")
    terminal.mark()
    terminal.feed(b"\x1b[2J\x1b[Hnew")
    assert terminal.since_mark == "new"
    assert terminal.text == "old\nnew"


def test_a_mark_past_the_end_of_the_line() -> None:
    terminal = rendered(b"> \x1b[3C")
    terminal.mark()
    terminal.feed(b"abc")
    assert terminal.since_mark == "abc"
    assert terminal.text == ">    abc"


def test_views_are_kept_until_the_next_change() -> None:
    terminal = rendered(b"abc")
    assert terminal.text is terminal.text
    terminal.mark()
    assert terminal.since_mark is terminal.since_mark
    before = terminal.text
    terminal.feed(b"d")
    assert terminal.text == "abcd"
    assert before == "abc"


# ---------------------------------------------------------------- keys


def test_named_keys() -> None:
    assert encode_key("up") == b"\x1b[A"
    assert encode_key("down") == b"\x1b[B"
    assert encode_key("right") == b"\x1b[C"
    assert encode_key("left") == b"\x1b[D"
    assert encode_key("home") == b"\x1b[H"
    assert encode_key("end") == b"\x1b[F"
    assert encode_key("insert") == b"\x1b[2~"
    assert encode_key("delete") == b"\x1b[3~"
    assert encode_key("page_up") == b"\x1b[5~"
    assert encode_key("page_down") == b"\x1b[6~"
    assert encode_key("tab") == b"\t"
    assert encode_key("enter") == b"\r"
    assert encode_key("backspace") == b"\x7f"
    assert encode_key("escape") == b"\x1b"
    assert encode_key("ctrl-a") == b"\x01"
    assert encode_key("ctrl-c") == b"\x03"
    assert encode_key("ctrl-z") == b"\x1a"
    assert [encode_key(f"ctrl-{letter}")[0] for letter in "abcdefghijklmnopqrstuvwxyz"] == list(
        range(1, 27)
    )
    assert len(KEYS) == 40


def test_an_unknown_key_lists_the_keys() -> None:
    with pytest.raises(ValueError, match=r"no key named 'Up'; the keys are up, down, .*, escape"):
        encode_key("Up")
    with pytest.raises(TypeError):
        KEYS["f1"] = b"\x1bOP"  # type: ignore[index]


# ---------------------------------------------------------------- speed


def _stream(size: int) -> bytes:
    """Console traffic: colored log lines, a prompt, edits and a recall."""
    unit = (
        b"\x1b[32m[INFO]\x1b[0m t=1234 sensor 3 reads 0x1A2B\r\n"
        + PROMPT
        + b"stauts\x1b[D\x1b[D\x1b[D\x1b[P\x1b[C\x1b[@u\x1b[3C"
        + b"\r"
        + b" " * 11
        + b"\r"
        + PROMPT
        + b"help\r\n\tcommand\tdescription\r\nprogress [####]\r\x1b[K"
    )
    return (unit * (size // len(unit) + 1))[:size]


def _feed_time(data: bytes) -> float:
    best = float("inf")
    for _ in range(3):
        terminal = Terminal()
        start = time.perf_counter()
        for offset in range(0, len(data), 4096):
            terminal.feed(data[offset : offset + 4096])
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.slow
def test_feed_time_grows_in_proportion_to_its_input() -> None:
    small = _feed_time(_stream(512 * 1024))
    large = _feed_time(_stream(4 * 512 * 1024))
    assert large < 8 * small  # four times the input; a quadratic cost would be sixteen
