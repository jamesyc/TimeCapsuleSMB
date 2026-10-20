"""tests/samba/tdb_records.py against real smbXsrv_open_global records.

The record values come from the patched smbd in Docker (issue 384's
experiments): an open disconnected by a dropped connection, one owned by an
smbd that was killed (pid 118), and a replay cache record. The tdb around them
is built here, in both byte orders, with the free and dead records a real
file holds between live ones, linked as tdb links them.
"""
import struct

import pytest

from tests.samba import tdb_records

DISC_KEY = bytes.fromhex('d2a9f7c6')
DISC_VALUE = bytes.fromhex(
    '010000000300000001000000f1aef1aeffffffffffffffffffffffffffffffffffffffffffffffff0000000000000000'
    '00000000c6f7a9d2c6f7a9d200000000b57dbd3400000000000000000000000000000000000000000000000000000000'
    '00000000010500000000000515000000cb375979506cc5a52a42594be803000090500dc3c45fdd010d635936af224bfb'
    '813cca1f7fec8cd3d3b908b9c5bc454eadcd77f4996d554f000000000000000000000000000000000000000000000000'
    '000000000000000002000000ec6e4cc3c45fdd01e0930400010000003e0100005646535f44454641554c545f44555241'
    '424c455f434f4f4b49455f4d414749432020202020202020202020202020202001000000010000003500000000000000'
    '550f03000000000000000000000000000000020004000200000000000000000000000000000000000000000000000000'
    '3500000000000000550f030000000000e481000000000000010000000000000000000000000000000000000000000000'
    '00000000000000000000000000000000f20bd66a0000000070bdd20900000000f20bd66a0000000070bdd20900000000'
    'f20bd66a00000000f041f10900000000f20bd66a0000000070bdd2090000000000100000000000000000000000000000'
    '00000000000000000b000000000000000b0000002f7372762f7368617265000012000000000000001200000074313739'
    '323431323635372f73303032310000000000000000000000ffffffffffffffffffffffffffffffffffffffffffffffff'
    'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
)
DEAD_KEY = bytes.fromhex('60cc7466')
DEAD_VALUE = bytes.fromhex(
    '010000000200000001000000f1aef1ae760000000000000000000000ffffffff5c0c3aed1e7940bd76350f7400000000'
    '4696ca176674cc606674cc6000000000da9fd33e00000000000000000000000000000000000000000000000000000000'
    '00000000010500000000000515000000cb375979506cc5a52a42594be8030000faf2e2ffc35fdd0159766a1cb7584bff'
    '9401a071e727c8ba556d787c74fa4fbf98543c3ce956bfdf000000000000000000000000000000000000000000000000'
    '0000000000000000020000000000000000000000e0930400010000003e0100005646535f44454641554c545f44555241'
    '424c455f434f4f4b49455f4d414749432020202020202020202020202020202001000000000000003500000000000000'
    '3e0f03000000000000000000000000000000020004000200000000000000000000000000000000000000000000000000'
    '35000000000000003e0f030000000000e481000000000000010000000000000000000000000000000000000000000000'
    '00000000000000000000000000000000aa0ad66a0000000061a7952b00000000aa0ad66a0000000061a7952b00000000'
    'aa0ad66a0000000061a7952b00000000aa0ad66a0000000060a7952b0000000000100000000000000000000000000000'
    '00000000000000000b000000000000000b0000002f7372762f7368617265000012000000000000001200000074313739'
    '323431323332392f66303034390000000000000000000000ffffffffffffffffffffffffffffffffffffffffffffffff'
    'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
)
REPLAY_KEY = bytes.fromhex('d3b908b9c5bc454eadcd77f4996d554f0505e667671d4b0dbdf8e2ad6f69c51a')
REPLAY_VALUE = bytes.fromhex(
    'cecca038'
)

FREE_MAGIC = tdb_records.TDB_FREE_MAGIC
DEAD_MAGIC = tdb_records.TDB_DEAD_MAGIC


def tdb(entries, order="<", hash_size=7):
    """A tdb file: header, hash heads, then each entry as (magic, key,
    value, padding), back to back. As tdb does, live
    and dead records are linked into a hash chain (the first here) and free
    ones into the free list, through the heads and each record's next."""
    data = bytearray(168 + 4 * (hash_size + 1))
    data[:8] = b"TDB file"
    struct.pack_into(order + "II", data, 32, tdb_records.TDB_VERSION, hash_size)
    placed = []
    for magic, key, value, padding in entries:
        rec_len = len(key) + len(value) + padding
        placed.append((len(data), magic))
        data += struct.pack(order + "6I", 0, rec_len, len(key), len(value), 0, magic)
        data += key + value + bytes(padding)
    for head, magics in ((168, {FREE_MAGIC}), (172, {tdb_records.TDB_MAGIC, DEAD_MAGIC})):
        link = head
        for offset, magic in placed:
            if magic in magics:
                struct.pack_into(order + "I", data, link, offset)
                link = offset  # a record's next is its first field
    return bytes(data)


LIVE = [(tdb_records.TDB_MAGIC, DISC_KEY, DISC_VALUE, 4),
        (FREE_MAGIC, b"", b"", 64),
        (tdb_records.TDB_MAGIC, DEAD_KEY, DEAD_VALUE, 0),
        (DEAD_MAGIC, DEAD_KEY, DEAD_VALUE, 0),
        (tdb_records.TDB_MAGIC, REPLAY_KEY, REPLAY_VALUE, 12)]


@pytest.mark.parametrize("order", ["<", ">"], ids=["little-endian", "big-endian"])
def test_live_records_in_order_whatever_lies_between(order):
    found = list(tdb_records.records(tdb(LIVE, order)))
    assert found == [(DISC_KEY, DISC_VALUE), (DEAD_KEY, DEAD_VALUE), (REPLAY_KEY, REPLAY_VALUE)]


def test_opens_tell_disconnected_from_owned_and_skip_replay_records():
    opens = tdb_records.opens(tdb(LIVE))
    assert [(hex(o.open_id), o.disconnected) for o in opens] == [("0xd2a9f7c6", True), ("0x60cc7466", False)]
    disconnected, owned = opens
    assert owned.pid == 118 and owned.disconnect_time == 0
    # Disconnected at 2026-10-19 12:24:18 UTC, as NTTIME.
    assert disconnected.pid == tdb_records.DISCONNECTED_PID
    assert disconnected.disconnect_time == 134368862585777900
    # A replay key is the client GUID, then the create GUID; this replay
    # record belongs to the disconnected open's client, the other open's
    # came from another run.
    assert REPLAY_KEY[:16] == disconnected.client_guid != owned.client_guid
    assert tdb_records.open_record(REPLAY_KEY, REPLAY_VALUE) is None


def test_lease_clients_are_the_first_half_of_each_key():
    lease = (tdb_records.TDB_MAGIC, bytes(range(32)), b"\x01" * 40, 0)
    assert tdb_records.lease_clients(tdb([lease, lease])) == [bytes(range(16))] * 2


# A copy that is not a whole tdb (a failed or short read, another file) must
# raise: the device suites wait for "no records" as proof of a cleanup.
@pytest.mark.parametrize("data", [b"", b"TDB file", tdb(LIVE)[:100]], ids=["empty", "magic-only", "short-header"])
def test_a_file_shorter_than_its_header_is_invalid(data):
    with pytest.raises(tdb_records.InvalidTdb, match="no tdb header"):
        list(tdb_records.records(data))


def test_a_file_without_the_tdb_magic_is_invalid():
    data = b"XDB file" + tdb(LIVE)[8:]
    with pytest.raises(tdb_records.InvalidTdb, match="no tdb header"):
        list(tdb_records.records(data))


def test_an_unknown_version_in_either_byte_order_is_invalid():
    data = bytearray(tdb(LIVE))
    struct.pack_into("<I", data, 32, tdb_records.TDB_VERSION + 1)
    with pytest.raises(tdb_records.InvalidTdb, match="unknown tdb version"):
        list(tdb_records.records(bytes(data)))


def test_hash_chains_past_the_end_are_invalid():
    with pytest.raises(tdb_records.InvalidTdb, match="hash chains do not fit"):
        list(tdb_records.records(tdb([], hash_size=1000)[:400]))


@pytest.mark.parametrize("order", ["<", ">"], ids=["little-endian", "big-endian"])
def test_a_copy_cut_inside_a_record_is_invalid_after_the_whole_records(order):
    data = tdb(LIVE, order)
    cut = data[:-10]
    found = []
    with pytest.raises(tdb_records.InvalidTdb, match="runs past the end"):
        for record in tdb_records.records(cut):
            found.append(record)
    assert found == [(DISC_KEY, DISC_VALUE), (DEAD_KEY, DEAD_VALUE)]


@pytest.mark.parametrize("order", ["<", ">"], ids=["little-endian", "big-endian"])
def test_every_copy_cut_short_is_invalid(order):
    # A cut between records walks cleanly, and one inside the last header
    # leaves no header to read: neither may pass for a smaller database.
    data = tdb(LIVE, order)
    assert len(list(tdb_records.records(data))) == 3
    for cut in range(168, len(data)):
        with pytest.raises(tdb_records.InvalidTdb):
            list(tdb_records.records(data[:cut]))


@pytest.mark.parametrize("cut", [200, 212], ids=["at-the-first-record", "inside-its-header"])
def test_a_copy_cut_at_or_just_past_the_first_record_is_invalid(cut):
    # The heads point at records the copy no longer holds.
    with pytest.raises(tdb_records.InvalidTdb, match=r"points to \d+, past the end"):
        list(tdb_records.records(tdb(LIVE)[:cut]))


def test_an_empty_record_header_is_invalid():
    # Zeros where a record should start: a copy torn while tdb grew the file.
    data = tdb(LIVE) + bytes(24)
    with pytest.raises(tdb_records.InvalidTdb, match="empty record header"):
        list(tdb_records.records(data))


def test_a_copy_cut_inside_a_trailing_header_is_invalid():
    # Room for no header after the last whole record.
    data = tdb(LIVE) + bytes(4) + struct.pack("<I", 1)
    with pytest.raises(tdb_records.InvalidTdb, match="copy ends inside a record header"):
        list(tdb_records.records(data))


def test_a_recovery_area_past_the_end_is_invalid():
    data = bytearray(tdb(LIVE))
    struct.pack_into("<I", data, 44, len(data))
    with pytest.raises(tdb_records.InvalidTdb, match="the recovery area points"):
        list(tdb_records.records(bytes(data)))


def test_a_live_record_holding_more_than_its_length_is_invalid():
    data = bytearray(tdb([(tdb_records.TDB_MAGIC, DISC_KEY, DISC_VALUE, 0)]))
    offset = 168 + 4 * 8
    struct.pack_into("<I", data, offset + 4, len(DISC_KEY) + len(DISC_VALUE) - 1)
    with pytest.raises(tdb_records.InvalidTdb, match="holds more than"):
        list(tdb_records.records(bytes(data)))


@pytest.mark.parametrize("value", [b"\x02" + DISC_VALUE[1:], DISC_VALUE[:0x60], DISC_VALUE[:0xcc + 7]],
                         ids=["version-2", "before-the-owner-sid", "before-disconnect-time"])
def test_an_open_record_of_another_version_or_cut_short_is_invalid(value):
    with pytest.raises(tdb_records.InvalidTdb):
        tdb_records.open_record(DISC_KEY, value)
