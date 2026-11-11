"""File paths in monitor commands (``jig.renode.session.monitor_path``):
absolute, in forward slashes and double quotes, since the monitor ends an
``@path`` token at the first space. The Renode tests of storage run boards
whose paths hold spaces (``test_storage_renode``)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.renode.session import monitor_path


def test_a_path_goes_in_double_quotes_absolute_and_in_forward_slashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "my runs" / "flash0.img"
    assert monitor_path(image) == f'"{image.as_posix()}"'
    monkeypatch.chdir(tmp_path)
    expected = (tmp_path / "a dir" / "a file.bin").as_posix()
    assert monitor_path("a dir/a file.bin") == f'"{expected}"'  # Renode's directory differs


@pytest.mark.parametrize("name", ['a "quoted" name.bin', "a\nline break.bin", "a\ttab.bin"])
def test_a_path_the_monitor_cannot_quote_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match=r"^Renode's monitor can't take the path .*double quote"):
        monitor_path(tmp_path / name)


def test_a_path_ending_on_a_backslash_is_refused(tmp_path: Path) -> None:
    if os.name == "nt":  # a backslash separates; the quoted path has none
        assert "\\" not in monitor_path(tmp_path / "a\\b")
    else:  # a backslash is part of the name, and would escape the closing quote
        with pytest.raises(ConfigError, match=r"end on a backslash"):
            monitor_path(tmp_path / "a\\")
