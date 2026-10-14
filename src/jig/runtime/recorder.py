"""The run recorder: everything a run produces, streamed to its directory as it
happens, so a long mission keeps a bounded memory and a run that stops part
way still leaves its data behind::

    events.jsonl          simulation events: boot, power, reset, sensor, rtc, link
    steps.jsonl           every step the interpreter logged, with its block
    telemetry.csv         every record value: time, block, name, value
    <other>.csv           tables extensions write (Recorder.table), such as the
                          CSP extension's <bus>.packets.csv

Files appear when their first line is written. Lines are flushed at each
simulation step boundary the harness reaches (``flush``) and when the run ends.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import IO, Any


class Recorder:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._files: dict[str, IO[str]] = {}
        self._csv: dict[str, Any] = {}
        self.counts: dict[str, int] = {}

    # ------------------------------------------------------------ writers

    def event(self, time_ns: int, source: str, kind: str, detail: str) -> None:
        self._json(
            "events.jsonl", {"t": time_ns / 1e9, "source": source, "kind": kind, "detail": detail}
        )

    def step(self, block: str, step: Any) -> None:
        self._json(
            "steps.jsonl",
            {
                "t": step.time,
                "block": block,
                "task": step.task,
                "where": step.where,
                "kind": step.kind,
                "text": step.text,
                "outcome": step.outcome,
                "detail": step.detail,
            },
        )

    def record(self, block: str, name: str, time: float, value: Any) -> None:
        self._row(
            "telemetry.csv", ["t", "block", "name", "value"], [time, block, name, _plain(value)]
        )

    def table(self, name: str, header: list[str], row: list[Any]) -> None:
        """One row of the CSV file ``name``; the header goes first, once."""
        reserved = ("events.jsonl", "steps.jsonl", "telemetry.csv")
        if name in reserved or not name.endswith(".csv") or "/" in name or "\\" in name:
            raise ValueError(f"{name!r} is not a table name such as 'can0.packets.csv'")
        self._row(name, header, row)

    # ------------------------------------------------------------- files

    @property
    def files(self) -> dict[str, Path]:
        return {name: self.directory / name for name in self._files}

    def flush(self) -> None:
        for handle in self._files.values():
            handle.flush()

    def close(self) -> None:
        for handle in self._files.values():
            handle.close()
        self._files.clear()
        self._csv.clear()

    def _open(self, name: str) -> IO[str]:
        handle = self._files.get(name)
        if handle is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            handle = (self.directory / name).open("w", encoding="utf-8", newline="")
            self._files[name] = handle
        self.counts[name] = self.counts.get(name, 0) + 1
        return handle

    def _json(self, name: str, entry: dict[str, Any]) -> None:
        self._open(name).write(json.dumps(entry, default=_plain) + "\n")

    def _row(self, name: str, header: list[str], row: list[Any]) -> None:
        new = name not in self._files
        handle = self._open(name)
        writer = self._csv.get(name)
        if writer is None:
            writer = csv.writer(handle, lineterminator="\n")
            self._csv[name] = writer
        if new:
            writer.writerow(header)
        writer.writerow(row)


def _plain(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "plain"):
        return value.plain()
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return repr(value)
