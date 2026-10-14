"""CSP 1.6 framing: header, CRC-32C, KISS and the CAN fragmentation protocol."""

from __future__ import annotations

import struct

import pytest

from jig.ext.csp import protocol as csp


def test_crc32c_check_value() -> None:
    # CRC-32/ISCSI (Castagnoli) check value from the CRC RevEng catalogue.
    assert csp.crc32c(b"123456789") == 0xE3069283


def test_header_layout() -> None:
    header = csp.CspId(src=0, dst=3, dport=csp.PORT_PING, sport=48)
    # priority 2 [31:30], src [29:25], dst [24:20], dport [19:14], sport [13:8]
    assert header.pack() == struct.pack(">I", (2 << 30) | (3 << 20) | (1 << 14) | (48 << 8))
    assert csp.CspId.unpack(header.pack()) == header


def test_header_rejects_out_of_range_fields() -> None:
    with pytest.raises(ValueError):
        csp.CspId(src=32, dst=0, dport=0, sport=0)
    with pytest.raises(ValueError):
        csp.CspId(src=0, dst=0, dport=64, sport=0)


def test_reply_swaps_addresses_and_ports() -> None:
    request = csp.CspId(src=8, dst=3, dport=1, sport=50, flags=csp.FLAG_CRC32)
    assert request.reply() == csp.CspId(src=3, dst=8, dport=50, sport=1, flags=csp.FLAG_CRC32)


def test_kiss_round_trip_with_escaped_bytes() -> None:
    payload = bytes([0x00, csp.FEND, 0x41, csp.FESC, csp.FEND, csp.FESC])
    packet = csp.Packet(csp.CspId(src=8, dst=3, dport=7, sport=33), payload)
    frame = csp.kiss_encode(packet)
    assert frame[0] == csp.FEND and frame[1] == csp.TNC_DATA and frame[-1] == csp.FEND
    assert csp.FEND not in frame[1:-1]
    decoder = csp.KissDecoder()
    assert list(decoder.feed(frame)) == [packet]


def test_kiss_carries_crc_of_the_payload_only() -> None:
    packet = csp.Packet(csp.CspId(src=3, dst=8, dport=33, sport=1), b"abc")
    frame = csp.kiss_encode(packet)
    body = frame[2:-1]  # no escapes in this frame
    assert body[-4:] == struct.pack(">I", csp.crc32c(b"abc"))


def test_kiss_decoder_drops_bad_crc_and_resynchronises() -> None:
    good = csp.Packet(csp.CspId(src=3, dst=8, dport=33, sport=1), b"ok")
    corrupt = bytearray(csp.kiss_encode(good))
    corrupt[-3] ^= 0xFF  # inside the CRC
    decoder = csp.KissDecoder()
    stream = b"noise" + bytes(corrupt) + csp.kiss_encode(good)
    assert list(decoder.feed(stream)) == [good]
    assert decoder.crc_errors == 1


def test_kiss_decoder_accepts_split_input() -> None:
    packet = csp.Packet(csp.CspId(src=3, dst=8, dport=33, sport=1), bytes(range(40)))
    frame = csp.kiss_encode(packet)
    decoder = csp.KissDecoder()
    received = []
    for index in range(len(frame)):
        received += list(decoder.feed(frame[index : index + 1]))
    assert received == [packet]


@pytest.mark.parametrize("length", [0, 1, 2, 3, 9, 10, 11, 64, 200, csp.CFP_MAX_PAYLOAD])
def test_cfp_fragmentation_matches_libcsp(length: int) -> None:
    packet = csp.Packet(
        csp.CspId(src=3, dst=0, dport=7, sport=7), bytes(i & 0xFF for i in range(length))
    )
    frames = csp.cfp_fragments(packet, ident=0x155)
    # csp_can_tx: remain = (length + CFP_OVERHEAD - 1) / 8 on the first frame
    first_id, first_data = frames[0]
    src, dst, frame_type, remain, ident = csp.cfp_fields(first_id)
    assert (src, dst, frame_type, ident) == (3, 0, csp.CFP_BEGIN, 0x155)
    assert remain == (length + csp.CFP_OVERHEAD - 1) // 8
    assert first_data[:4] == packet.id.pack()
    assert struct.unpack(">H", first_data[4:6])[0] == length
    assert len(frames) == 1 + remain
    for index, (frame_id, data) in enumerate(frames[1:], start=1):
        _, _, frame_type, frame_remain, _ = csp.cfp_fields(frame_id)
        assert frame_type == csp.CFP_MORE
        assert frame_remain == remain - index
        assert 1 <= len(data) <= 8
    reassembler = csp.CfpReassembler()
    results = [reassembler.feed(frame_id, data) for frame_id, data in frames]
    assert results[-1] == packet
    assert all(result is None for result in results[:-1])


def test_cfp_rejects_oversized_packets() -> None:
    packet = csp.Packet(csp.CspId(src=3, dst=0, dport=7, sport=7), bytes(csp.CFP_MAX_PAYLOAD + 1))
    with pytest.raises(ValueError):
        csp.cfp_fragments(packet, ident=1)


def test_cfp_drops_a_transfer_with_a_missing_frame() -> None:
    packet = csp.Packet(csp.CspId(src=3, dst=0, dport=7, sport=7), bytes(40))
    frames = csp.cfp_fragments(packet, ident=7)
    reassembler = csp.CfpReassembler()
    for frame_id, data in frames[:2] + frames[3:]:
        assert reassembler.feed(frame_id, data) is None
    assert reassembler.dropped >= 1


def test_cfp_via_address_goes_into_the_frame_only() -> None:
    packet = csp.Packet(csp.CspId(src=3, dst=8, dport=7, sport=7), b"x")
    frame_id, data = csp.cfp_fragments(packet, ident=0, via=0)[0]
    assert csp.cfp_fields(frame_id)[1] == 0
    assert csp.CspId.unpack(data[:4]).dst == 8
