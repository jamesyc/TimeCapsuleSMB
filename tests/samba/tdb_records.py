"""Records of a tdb file copied off a device, and what an open record says.

The device suites read /mnt/Locks this way to check what Samba keeps there
after a client goes away (issue 384): the devices have no tdbdump. tdb files
are in the writing host's byte order (a big-endian NetBSD 4 writes big-endian
headers); the NDR inside the records is always little-endian.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

TDB_FILE = b"TDB file"  # the start of struct tdb_header's magic_food
TDB_MAGIC = 0x26011999
TDB_FREE_MAGIC = ~TDB_MAGIC & 0xFFFFFFFF
TDB_DEAD_MAGIC = 0xFEE1DEAD
TDB_VERSION = 0x26011967 + 6  # tdb.h: (0x26011967 + 6)
HEADER_SIZE = 168  # struct tdb_header, then the free list and hash chain heads
RECOVERY_START = 44  # struct tdb_header's recovery_start
RECORD_SIZE = 24  # struct tdb_record: next, rec_len, key_len, data_len, full_hash, magic
DISCONNECTED_PID = 2**64 - 1  # server_id_set_disconnected()
SMBXSRV_VERSION_1 = struct.pack("<I", 1)  # smbXsrv.idl's SMBXSRV_VERSION_1


class InvalidTdb(ValueError):
    """The bytes are not a whole tdb file: a read that failed or came back
    short, or another file. Callers must not take it for an empty one."""


def records(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    """(key, value) of every live record, in file order. Free and dead
    records are skipped; so is the recovery area at the end, whose records
    carry another magic. Raises InvalidTdb for anything but a whole tdb.

    A copy cut short can end exactly between records and still walk cleanly,
    so every offset the file holds must point inside it: the free list and
    hash chain heads, the recovery area, and each record's next. A whole
    file ends where its last record does (an expansion adds a free record
    up to the new end), so a walk that stops short of the end was cut too."""
    if len(data) < HEADER_SIZE or not data.startswith(TDB_FILE):
        raise InvalidTdb(f"no tdb header in {len(data)} bytes")
    if struct.unpack_from("<I", data, 32)[0] == TDB_VERSION:
        order = "<"
    elif struct.unpack_from(">I", data, 32)[0] == TDB_VERSION:
        order = ">"
    else:
        raise InvalidTdb(f"unknown tdb version {data[32:36].hex()}")
    hash_size = struct.unpack_from(order + "I", data, 36)[0]
    offset = HEADER_SIZE + 4 * (hash_size + 1)
    if offset > len(data):
        raise InvalidTdb(f"{hash_size} hash chains do not fit in {len(data)} bytes")

    def inside(target: int, what: str) -> None:
        if target and target + RECORD_SIZE > len(data):
            raise InvalidTdb(f"{what} points to {target}, past the end of {len(data)} bytes")

    for i, head in enumerate(struct.unpack_from(f"{order}{hash_size + 1}I", data, HEADER_SIZE)):
        inside(head, "the free list" if i == 0 else f"hash chain {i - 1}")
    inside(struct.unpack_from(order + "I", data, RECOVERY_START)[0], "the recovery area")
    while offset + RECORD_SIZE <= len(data):
        next_offset, rec_len, key_len, data_len, _, magic = struct.unpack_from(order + "6I", data, offset)
        # Records follow each other with no gap, free space in free records:
        # an empty header is a copy torn or not a tdb.
        if not rec_len and magic != TDB_MAGIC:
            raise InvalidTdb(f"empty record header at {offset} of {len(data)} bytes")
        if offset + RECORD_SIZE + rec_len > len(data):
            raise InvalidTdb(f"record at {offset} runs past the end of {len(data)} bytes")
        if magic in (TDB_MAGIC, TDB_DEAD_MAGIC, TDB_FREE_MAGIC):
            inside(next_offset, f"record at {offset}")
        if magic == TDB_MAGIC:
            if key_len + data_len > rec_len:
                raise InvalidTdb(f"record at {offset} holds more than its {rec_len} bytes")
            start = offset + RECORD_SIZE
            yield data[start:start + key_len], data[start + key_len:start + key_len + data_len]
        offset += RECORD_SIZE + rec_len
    if offset != len(data):
        raise InvalidTdb(f"copy ends inside a record header at {offset} of {len(data)} bytes")


@dataclass(frozen=True)
class Open:
    """The parts of an smbXsrv_open_global record (version 1 NDR) the suites
    check: who owns it, and since when it is disconnected (0 when live)."""
    open_id: int
    pid: int
    disconnect_time: int
    client_guid: bytes

    @property
    def disconnected(self) -> bool:
        return self.pid == DISCONNECTED_PID


def open_record(key: bytes, value: bytes) -> Open | None:
    """The open in an smbXsrv_open_global.tdb record, or None for a replay
    cache record (a 32-byte client and create GUID key)."""
    if len(key) != 4:
        return None
    # smbXsrv_open_globalB: version, seqnum, union pointer; the
    # smbXsrv_open_global struct starts at the next 8-byte boundary.
    if len(value) < 0x66 or value[:4] != SMBXSRV_VERSION_1:
        raise InvalidTdb(f"open record of {len(value)} bytes is not a version 1 smbXsrv_open_globalB")
    pid = struct.unpack_from("<Q", value, 0x10)[0]
    # file_id ends at 0x60, then name_hash and the open owner's SID, whose
    # size follows its sub-authority count.
    sub_auths = value[0x65]
    offset = (0x64 + 8 + 4 * sub_auths + 3) & ~3
    client_guid = value[offset + 8 + 16:offset + 8 + 32]
    # open_time, create, client and app instance GUIDs, the two app instance
    # versions and create_action come before disconnect_time.
    offset += 8 + 3 * 16 + 8 + 8 + 4
    if offset + 8 > len(value):
        raise InvalidTdb(f"open record of {len(value)} bytes ends before its disconnect time")
    disconnect_time = struct.unpack_from("<Q", value, offset)[0]
    return Open(struct.unpack(">I", key)[0], pid, disconnect_time, client_guid)


def opens(data: bytes) -> list[Open]:
    return [o for o in (open_record(k, v) for k, v in records(data)) if o is not None]


def lease_clients(data: bytes) -> list[bytes]:
    """The client GUID of each leases.tdb record (the first half of its
    key, NDR like Open.client_guid)."""
    return [key[:16] for key, _ in records(data)]
