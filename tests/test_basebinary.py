from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.basebinary import (
    BasebinaryError,
    BasebinaryHeader,
    BasebinaryKey,
    DEFAULT_BASEBINARY_KEYS,
    compose_basebinary,
    compose_nested_basebinary,
    decrypt_basebinary_payload,
    derive_basebinary_key,
    encrypt_basebinary_payload,
    parse_basebinary,
    parse_nested_basebinary,
)


TEST_KEY = BasebinaryKey.from_hex("test-key", "00112233445566778899aabbccddeeff")
WRONG_KEY = BasebinaryKey.from_hex("wrong-key", "ffeeddccbbaa99887766554433221100")


def make_header(*, encrypted: bool, model: int = 999, version: int = 0x12345678) -> BasebinaryHeader:
    return BasebinaryHeader(
        iv_suffix=0x2E,
        model=model,
        version=version,
        byte_0x18=0x11,
        byte_0x19=0x22,
        byte_0x1a=0x33,
        flags=0x02 if encrypted else 0x00,
        unk_0x1c=0,
    )


class BasebinaryTests(unittest.TestCase):
    def test_derive_key_matches_acpd_observed_derivation(self) -> None:
        stored = bytes.fromhex("513c1ca5bf035127335f7c2596aa20aa")

        self.assertEqual(derive_basebinary_key(stored).hex(), "482607b9a21d4e07127d5f01b38c0782")

    def test_observed_k30a_key_is_in_default_trial_keyring(self) -> None:
        key = next(key for key in DEFAULT_BASEBINARY_KEYS if key.key_id == "observed-k30a-78100")

        self.assertEqual(key.stored_key.hex(), "c025fefa2320b0e985dfac106694db4a")
        self.assertEqual(key.derived_key.hex(), "d93fe5e63e3eafc9a4fd8f3443b2fc62")

    def test_observed_k30b_key_is_in_default_trial_keyring(self) -> None:
        key = next(key for key in DEFAULT_BASEBINARY_KEYS if key.key_id == "observed-k30b-78100")

        self.assertEqual(key.stored_key.hex(), "9d1259ee89f28a2ccfa64697adbb4193")
        self.assertEqual(key.derived_key.hex(), "840842f294ec950cee8465b3889d66bb")

    def test_default_keyring_parses_observed_k30b_model_116_container(self) -> None:
        key = next(key for key in DEFAULT_BASEBINARY_KEYS if key.key_id == "observed-k30b-78100")
        payload = b"model 116 firmware payload" * 128
        encoded = compose_basebinary(make_header(encrypted=True, model=116, version=0x07818000), payload, key=key)

        parsed = parse_basebinary(encoded)

        self.assertEqual(parsed.key_id, "observed-k30b-78100")
        self.assertEqual(parsed.header.model, 116)
        self.assertEqual(parsed.payload, payload)

    def test_observed_j28_key_is_in_default_trial_keyring(self) -> None:
        key = next(key for key in DEFAULT_BASEBINARY_KEYS if key.key_id == "observed-j28-79100")

        self.assertEqual(key.stored_key.hex(), "b19937ddcb78b3f151e4e0b48198e6a7")
        self.assertEqual(key.derived_key.hex(), "a8832cc1d666acd170c6c390a4bec18f")

    def test_unencrypted_container_round_trips_with_checksum(self) -> None:
        payload = b"plain firmware payload"
        encoded = compose_basebinary(make_header(encrypted=False), payload)

        parsed = parse_basebinary(encoded, keys=())

        self.assertFalse(parsed.encrypted)
        self.assertIsNone(parsed.key)
        self.assertEqual(parsed.payload, payload)
        self.assertEqual(parsed.header.model, 999)

    def test_encrypted_container_tries_keys_by_checksum_not_model(self) -> None:
        payload = b"encrypted firmware payload" * 4
        encoded = compose_basebinary(make_header(encrypted=True, model=12345), payload, key=TEST_KEY)

        parsed = parse_basebinary(encoded, keys=(WRONG_KEY, TEST_KEY))

        self.assertTrue(parsed.encrypted)
        self.assertEqual(parsed.key_id, "test-key")
        self.assertEqual(parsed.header.model, 12345)
        self.assertEqual(parsed.payload, payload)

    def test_encrypted_container_refuses_when_no_candidate_key_validates(self) -> None:
        payload = b"encrypted firmware payload" * 4
        encoded = compose_basebinary(make_header(encrypted=True), payload, key=TEST_KEY)

        with self.assertRaises(BasebinaryError) as raised:
            parse_basebinary(encoded, keys=(WRONG_KEY,))

        self.assertIn("no candidate basebinary key", str(raised.exception))

    def test_encryption_resets_cbc_for_each_chunk_and_leaves_tail_raw(self) -> None:
        block = b"same first block"
        payload = block + (b"x" * (0x8000 - len(block))) + block + b"tail"
        header = make_header(encrypted=True)

        encrypted = encrypt_basebinary_payload(payload, key=TEST_KEY, iv=header.iv)

        self.assertEqual(encrypted[:16], encrypted[0x8000 : 0x8000 + 16])
        self.assertEqual(encrypted[-4:], b"tail")
        self.assertEqual(decrypt_basebinary_payload(encrypted, key=TEST_KEY, iv=header.iv), payload)

    def test_nested_basebinary_repack_preserves_headers_and_updates_inner_payload(self) -> None:
        original_payload = b"raw bank prefix" * 64
        inner_header = make_header(encrypted=True, model=106, version=0x07818000)
        outer_header = make_header(encrypted=False, model=106, version=0x07818000)
        inner = compose_basebinary(inner_header, original_payload, key=TEST_KEY)
        outer = compose_basebinary(outer_header, inner)
        template = parse_nested_basebinary(outer, keys=(TEST_KEY,))

        updated_payload = original_payload + b" patched"
        repacked = compose_nested_basebinary(template, updated_payload)
        reparsed = parse_nested_basebinary(repacked, keys=(TEST_KEY,))

        self.assertEqual(reparsed.outer.header, outer_header)
        self.assertEqual(reparsed.inner.header, inner_header)
        self.assertEqual(reparsed.inner.key_id, "test-key")
        self.assertEqual(reparsed.inner.payload, updated_payload)

    def test_nested_parser_refuses_non_nested_payload(self) -> None:
        encoded = compose_basebinary(make_header(encrypted=False), b"not nested")

        with self.assertRaises(BasebinaryError) as raised:
            parse_nested_basebinary(encoded, keys=(TEST_KEY,))

        self.assertIn("not another basebinary", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
