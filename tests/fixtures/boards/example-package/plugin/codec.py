"""The uptime service's reply (CSP port 6): whole seconds, a big-endian
32-bit word, as libcsp 1.6 answers it."""

from __future__ import annotations

from jig.plugin import HkContext, HkDecoder


def decode(payload: bytes) -> dict[str, object]:
    return {"uptime_s": int.from_bytes(payload[:4], "big")}


def uptime_codec(context: HkContext) -> HkDecoder:
    return decode
