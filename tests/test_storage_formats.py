"""The storage-format interface and the raw format."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.storage import formats


def test_raw_takes_no_arguments() -> None:
    raw = formats.get("raw")
    assert raw.arguments({}, 4096) == {}
    with pytest.raises(ConfigError, match="block_size"):
        raw.arguments({"block_size": 4096}, 4096)


def test_raw_contents_is_a_file_not_a_folder(tmp_path: Path) -> None:
    raw = formats.get("raw")
    with pytest.raises(ConfigError, match="not the folder"):
        raw.build(tmp_path, 4096, {})
    assert raw.check(b"\x00" * 16, {}) == []
    assert raw.open(b"\x00" * 16, {}) is None


def test_an_unknown_format_lists_the_known_ones() -> None:
    with pytest.raises(ConfigError, match=r"known: fat, littlefs, other, raw"):
        formats.get("missing", {"other": formats.Raw()})
    assert formats.get("other", {"other": formats.Raw()}).name == "raw"
