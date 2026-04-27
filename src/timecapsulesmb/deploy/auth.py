from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _MD4:
    A: int = 0x67452301
    B: int = 0xEFCDAB89
    C: int = 0x98BADCFE
    D: int = 0x10325476
    count: int = 0
    buffer: bytes = b""

    @staticmethod
    def _rol(value: int, bits: int) -> int:
        value &= 0xFFFFFFFF
        return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF

    @staticmethod
    def _f(x: int, y: int, z: int) -> int:
        return (x & y) | (~x & z)

    @staticmethod
    def _g(x: int, y: int, z: int) -> int:
        return (x & y) | (x & z) | (y & z)

    @staticmethod
    def _h(x: int, y: int, z: int) -> int:
        return x ^ y ^ z

    def update(self, data: bytes) -> None:
        self.count += len(data)
        self.buffer += data
        while len(self.buffer) >= 64:
            self._process(self.buffer[:64])
            self.buffer = self.buffer[64:]

    def _process(self, block: bytes) -> None:
        x = [int.from_bytes(block[i:i + 4], "little") for i in range(0, 64, 4)]
        a, b, c, d = self.A, self.B, self.C, self.D

        for k, s in ((0, 3), (1, 7), (2, 11), (3, 19), (4, 3), (5, 7), (6, 11), (7, 19),
                     (8, 3), (9, 7), (10, 11), (11, 19), (12, 3), (13, 7), (14, 11), (15, 19)):
            if k % 4 == 0:
                a = self._rol((a + self._f(b, c, d) + x[k]) & 0xFFFFFFFF, s)
            elif k % 4 == 1:
                d = self._rol((d + self._f(a, b, c) + x[k]) & 0xFFFFFFFF, s)
            elif k % 4 == 2:
                c = self._rol((c + self._f(d, a, b) + x[k]) & 0xFFFFFFFF, s)
            else:
                b = self._rol((b + self._f(c, d, a) + x[k]) & 0xFFFFFFFF, s)

        for k, s in ((0, 3), (4, 5), (8, 9), (12, 13), (1, 3), (5, 5), (9, 9), (13, 13),
                     (2, 3), (6, 5), (10, 9), (14, 13), (3, 3), (7, 5), (11, 9), (15, 13)):
            if k in (0, 1, 2, 3):
                a = self._rol((a + self._g(b, c, d) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)
            elif k in (4, 5, 6, 7):
                d = self._rol((d + self._g(a, b, c) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)
            elif k in (8, 9, 10, 11):
                c = self._rol((c + self._g(d, a, b) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)
            else:
                b = self._rol((b + self._g(c, d, a) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)

        order = [0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15]
        shifts = [3, 9, 11, 15] * 4
        for idx, k in enumerate(order):
            s = shifts[idx]
            if idx % 4 == 0:
                a = self._rol((a + self._h(b, c, d) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            elif idx % 4 == 1:
                d = self._rol((d + self._h(a, b, c) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            elif idx % 4 == 2:
                c = self._rol((c + self._h(d, a, b) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            else:
                b = self._rol((b + self._h(c, d, a) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)

        self.A = (self.A + a) & 0xFFFFFFFF
        self.B = (self.B + b) & 0xFFFFFFFF
        self.C = (self.C + c) & 0xFFFFFFFF
        self.D = (self.D + d) & 0xFFFFFFFF

    def digest(self) -> bytes:
        clone = _MD4(self.A, self.B, self.C, self.D, self.count, self.buffer)
        bit_len = clone.count * 8
        clone.update(b"\x80")
        while len(clone.buffer) % 64 != 56:
            clone.buffer += b"\x00"
        clone.buffer += bit_len.to_bytes(8, "little")
        while clone.buffer:
            clone._process(clone.buffer[:64])
            clone.buffer = clone.buffer[64:]
        return (
            clone.A.to_bytes(4, "little")
            + clone.B.to_bytes(4, "little")
            + clone.C.to_bytes(4, "little")
            + clone.D.to_bytes(4, "little")
        )


def nt_hash_hex(password: str) -> str:
    md4 = _MD4()
    md4.update(password.encode("utf-16le"))
    return md4.digest().hex().upper()


def render_smbpasswd(username: str, password: str) -> tuple[str, str]:
    nt_hash = nt_hash_hex(password)
    lct = f"{int(time.time()):08X}"
    smbpasswd_line = f"root:0:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX:{nt_hash}:[U          ]:LCT-{lct}:\n"
    username_map = "!root = root\nroot = *\n"
    return smbpasswd_line, username_map
