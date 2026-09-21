from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CONVERTER = REPO_ROOT / "build" / "symlink-converter.c"

# Reaching the converter's static functions: rename its main() out of the way
# and include the whole file, the way tests/test_deploy_modules.py already does
# for mdns-advertiser.c. Keeping the renamed main() matters -- it is what
# references every static, so none of them trips -Wunused-function.
HARNESS_PROLOGUE = """
#include <stdio.h>
#include <string.h>

#define main symlink_converter_main
#include "{converter}"
#undef main
"""

# Built from the Minshall+French layout and a digest computed independently of
# the converter, so nothing here originates in xsym_format's own output.
GOLDEN_TARGET = b"plain.txt"
GOLDEN_MD5 = b"83c8b39ff7243bf968a9163ee66e2295"
GOLDEN_XSYM = (
    b"XSym\n"
    + b"0009\n"
    + GOLDEN_MD5
    + b"\n"
    + GOLDEN_TARGET
    + b"\n"
    + b" " * (1067 - 43 - len(GOLDEN_TARGET) - 1)
)


class SymlinkConverterHarness(unittest.TestCase):
    """Compiles build/symlink-converter.c on the host and exercises it."""

    # CommonCrypto's MD5 is deprecated on macOS and OpenSSL's is deprecated
    # from 3.0, so -Werror needs this one exemption to reach either of them.
    # Everything else stays on.
    CFLAGS = ["-Wall", "-Wextra", "-Werror", "-Wno-deprecated-declarations"]

    # CommonCrypto is in libSystem; OpenSSL needs libcrypto named explicitly.
    MD5_LIBS = [] if sys.platform == "darwin" else ["-lcrypto"]

    def setUp(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")
        if not self.md5_available():
            self.skipTest("no system MD5 header (CommonCrypto or OpenSSL)")

    def md5_available(self) -> bool:
        # Link, do not merely compile: Linux images carry <openssl/md5.h>
        # without libcrypto often enough that a header check passes and the
        # link then fails on MD5_Init. The probe therefore calls the functions
        # and links exactly as the tests do.
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.c"
            probe.write_text(
                "#if defined(__APPLE__)\n"
                "#include <CommonCrypto/CommonDigest.h>\n"
                "#define CTX CC_MD5_CTX\n"
                "#define INIT CC_MD5_Init\n"
                "#define FINAL CC_MD5_Final\n"
                "#else\n"
                "#include <openssl/md5.h>\n"
                "#define CTX MD5_CTX\n"
                "#define INIT MD5_Init\n"
                "#define FINAL MD5_Final\n"
                "#endif\n"
                "int main(void) {\n"
                "    CTX ctx;\n"
                "    unsigned char digest[16];\n"
                "    INIT(&ctx);\n"
                "    FINAL(digest, &ctx);\n"
                "    return digest[0] == 0;\n"
                "}\n"
            )
            proc = subprocess.run(
                ["cc", "-Wno-deprecated-declarations", str(probe), "-o", str(Path(tmp) / "probe")]
                + self.MD5_LIBS,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            return proc.returncode == 0

    def compile_c(self, body: str, name: str, extra_flags: list[str] | None = None) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        source = HARNESS_PROLOGUE.format(converter=CONVERTER.as_posix()) + body
        c_path = tmp / f"{name}.c"
        bin_path = tmp / name
        c_path.write_text(source)
        # No -std=: the converter calls lutimes(3), which a strict dialect hides
        # on both libcs.
        proc = subprocess.run(
            ["cc", *self.CFLAGS]
            + list(extra_flags or [])
            + [str(c_path), "-o", str(bin_path)]
            + self.MD5_LIBS,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return bin_path

    def run_probe(self, body: str, name: str, args: list[str] | None = None,
                  extra_flags: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        bin_path = self.compile_c(body, name, extra_flags)
        return subprocess.run(
            [str(bin_path), *(args or [])],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


class TestMd5(SymlinkConverterHarness):
    """md5_hex feeds the digest field, so pin it before trusting any body."""

    def test_md5_hex_matches_the_rfc_1321_test_vectors(self) -> None:
        body = r"""
int
main(void)
{
	static const char *const inputs[] = {
		"",
		"a",
		"abc",
		"message digest",
		"abcdefghijklmnopqrstuvwxyz",
		"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
		("1234567890123456789012345678901234567890"
		 "1234567890123456789012345678901234567890")
	};
	char hex[MD5_DIGEST_LENGTH * 2 + 1];
	size_t i;

	for (i = 0; i < sizeof(inputs) / sizeof(inputs[0]); i++) {
		md5_hex((const unsigned char *)inputs[i], strlen(inputs[i]), hex);
		printf("%s\n", hex);
	}
	return 0;
}
"""
        run = self.run_probe(body, "md5_rfc_vectors")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.split(),
            [
                "d41d8cd98f00b204e9800998ecf8427e",
                "0cc175b9c0f1b6a831c399e269772661",
                "900150983cd24fb0d6963f7d28e17f72",
                "f96b697d7cb7938d525a2f31aaf161d0",
                "c3fcd3d76192e4007dfb496cca67e13b",
                "d174ab98d277d9f5a5611c2c9f419d9f",
                "57edf4a22be3c955ac49da2e2107b67a",
            ],
        )

    def test_md5_hex_is_lowercase_hex_over_the_raw_target_bytes(self) -> None:
        # The measured digest of a file macOS itself wrote on the share (#24).
        body = r"""
int
main(void)
{
	char hex[MD5_DIGEST_LENGTH * 2 + 1];

	md5_hex((const unsigned char *)"plain.txt", 9, hex);
	printf("%s\n", hex);
	return 0;
}
"""
        run = self.run_probe(body, "md5_plain_txt")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), GOLDEN_MD5.decode())


class TestXsymFormat(SymlinkConverterHarness):
    """xsym_format builds the 1067-byte body macOS recognises, or nothing works."""

    def test_formats_a_known_target_byte_for_byte(self) -> None:
        body = r"""
int
main(void)
{
	unsigned char buf[XSYM_FILE_SIZE];

	if (xsym_format("plain.txt", 9, buf) != 0)
		return 1;
	fwrite(buf, 1, sizeof(buf), stdout);
	return 0;
}
"""
        bin_path = self.compile_c(body, "xsym_format_golden")
        run = subprocess.run([str(bin_path)], capture_output=True, check=False, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(GOLDEN_XSYM), 1067)
        self.assertEqual(run.stdout, GOLDEN_XSYM)

    def test_pads_with_spaces_and_never_writes_a_nul(self) -> None:
        # A NUL anywhere in the body makes the object unrecognisable to the
        # client. snprintf() into the buffer would leave one after each header
        # field, so byte 9 and byte 42 are the two that catch that mistake.
        body = r"""
int
main(void)
{
	unsigned char buf[XSYM_FILE_SIZE];
	size_t i;
	int nul = 0;

	if (xsym_format("plain.txt", 9, buf) != 0)
		return 1;
	for (i = 0; i < sizeof(buf); i++) {
		if (buf[i] == '\0')
			nul++;
	}
	printf("nul=%d newline_after_length=%d newline_after_digest=%d pad=%d\n",
	    nul, buf[9] == '\n', buf[42] == '\n', buf[1066] == ' ');
	return 0;
}
"""
        run = self.run_probe(body, "xsym_format_padding")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.strip(),
            "nul=0 newline_after_length=1 newline_after_digest=1 pad=1",
        )

    def test_trailing_newline_is_present_until_the_target_fills_the_body(self) -> None:
        # The newline after the target is part of the padding, so a 1024-byte
        # target leaves room for neither it nor any padding at all.
        body = r"""
int
main(void)
{
	unsigned char buf[XSYM_FILE_SIZE];
	char target[XSYM_MAX_TARGET + 1];

	memset(target, 'a', sizeof(target));

	if (xsym_format(target, 1023, buf) != 0)
		return 1;
	printf("1023 last=%d is_newline=%d\n", buf[1066], buf[1066] == '\n');

	if (xsym_format(target, XSYM_MAX_TARGET, buf) != 0)
		return 1;
	printf("1024 last=%d is_target=%d declared=%.4s\n",
	    buf[1066], buf[1066] == 'a', buf + XSYM_LEN_OFFSET);
	return 0;
}
"""
        run = self.run_probe(body, "xsym_format_newline_edge")
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = run.stdout.split("\n")
        self.assertEqual(lines[0], "1023 last=10 is_newline=1")
        self.assertEqual(lines[1], "1024 last=97 is_target=1 declared=1024")

    def test_declares_the_target_length_in_bytes_not_characters(self) -> None:
        # "кирилиця-ціль.txt" is 17 characters and 29 bytes in UTF-8. Declaring
        # the character count would put the client's readlink off the end.
        body = r"""
int
main(void)
{
	const char *target = "\xd0\xba\xd0\xb8\xd1\x80\xd0\xb8\xd0\xbb\xd0\xb8"
	    "\xd1\x86\xd1\x8f-\xd1\x86\xd1\x96\xd0\xbb\xd1\x8c.txt";
	unsigned char buf[XSYM_FILE_SIZE];
	size_t len = strlen(target);

	if (xsym_format(target, len, buf) != 0)
		return 1;
	printf("bytes=%zu declared=%.4s digest=%.32s\n",
	    len, buf + XSYM_LEN_OFFSET, buf + XSYM_MD5_OFFSET);
	return 0;
}
"""
        run = self.run_probe(body, "xsym_format_non_ascii")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.strip(),
            "bytes=29 declared=0029 digest=02de4b32f9103352779ad047aaf83809",
        )

    def test_refuses_a_target_longer_than_the_body_can_hold(self) -> None:
        body = r"""
int
main(void)
{
	unsigned char buf[XSYM_FILE_SIZE];
	char target[XSYM_MAX_TARGET + 2];

	memset(target, 'a', sizeof(target));
	printf("1024=%d 1025=%d\n",
	    xsym_format(target, XSYM_MAX_TARGET, buf),
	    xsym_format(target, XSYM_MAX_TARGET + 1, buf));
	return 0;
}
"""
        run = self.run_probe(body, "xsym_format_too_long")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "1024=0 1025=-1")


class TestXsymParse(SymlinkConverterHarness):
    """Size alone proves nothing: the production share holds 51 files that are
    exactly 1067 bytes and not symlinks, so magic, length and digest are all
    validated before a target is trusted."""

    PARSE_PROBE = r"""
static void
show(const char *label, unsigned char *buf)
{
	char target[XSYM_MAX_TARGET + 1];
	size_t tlen = 0;
	enum outcome out = xsym_parse(buf, target, sizeof(target), &tlen);

	printf("%s=%s\n", label, outcome_names[out]);
}

int
main(void)
{
	unsigned char buf[XSYM_FILE_SIZE];
	char target[XSYM_MAX_TARGET + 1];
	size_t tlen = 0;
	enum outcome out;

	xsym_format("plain.txt", 9, buf);
	out = xsym_parse(buf, target, sizeof(target), &tlen);
	printf("roundtrip=%s tlen=%zu target=%s\n",
	    outcome_names[out], tlen, target);

	xsym_format("plain.txt", 9, buf);
	buf[0] = 'Y';
	show("bad_magic", buf);

	xsym_format("plain.txt", 9, buf);
	buf[XSYM_LEN_OFFSET] = 'x';
	show("non_numeric_length", buf);

	xsym_format("plain.txt", 9, buf);
	buf[XSYM_LEN_OFFSET + 4] = 'X';
	show("no_newline_after_length", buf);

	xsym_format("plain.txt", 9, buf);
	memcpy(buf + XSYM_LEN_OFFSET, "9999", 4);
	show("length_out_of_range", buf);

	xsym_format("plain.txt", 9, buf);
	memcpy(buf + XSYM_LEN_OFFSET, "0008", 4);
	show("length_disagrees_with_body", buf);

	xsym_format("plain.txt", 9, buf);
	buf[XSYM_MD5_OFFSET] = (buf[XSYM_MD5_OFFSET] == 'a') ? 'b' : 'a';
	show("corrupt_digest", buf);

	xsym_format("", 0, buf);
	show("empty_target", buf);

	memset(buf, 'A', sizeof(buf));
	show("not_an_xsym_object", buf);

	return 0;
}
"""

    def test_validates_magic_length_and_digest(self) -> None:
        run = self.run_probe(self.PARSE_PROBE, "xsym_parse_cases")
        self.assertEqual(run.returncode, 0, run.stderr)
        parsed = dict(
            line.split("=", 1) for line in run.stdout.split("\n") if "=" in line and " " not in line
        )
        self.assertEqual(
            run.stdout.split("\n")[0], "roundtrip=converted tlen=9 target=plain.txt"
        )
        self.assertEqual(parsed["bad_magic"], "bad_magic")
        self.assertEqual(parsed["non_numeric_length"], "bad_length")
        self.assertEqual(parsed["no_newline_after_length"], "bad_length")
        self.assertEqual(parsed["length_out_of_range"], "bad_length")
        self.assertEqual(parsed["corrupt_digest"], "bad_md5")
        self.assertEqual(parsed["empty_target"], "empty_target")
        # A plain 1067-byte file -- a LICENSE can be exactly this size.
        self.assertEqual(parsed["not_an_xsym_object"], "bad_magic")

    def test_a_length_disagreeing_with_the_body_is_reported_as_a_bad_digest(self) -> None:
        # Not bad_length, which is the intuitive answer: the digest is computed
        # over the declared length, so it stops disagreeing bodies first.
        run = self.run_probe(self.PARSE_PROBE, "xsym_parse_length_mismatch")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("length_disagrees_with_body=bad_md5", run.stdout)

    def test_format_accepts_an_empty_target_that_parse_then_refuses(self) -> None:
        # Documented rather than asserted as correct: xsym_format only checks
        # the upper bound, so it emits a body declaring "0000" that xsym_parse
        # rejects. There is no reachable hole in the tool -- handle_symlink
        # records OUT_EMPTY_TARGET before xsym_format is ever called -- but the
        # two functions do not agree on their own.
        body = r"""
int
main(void)
{
	unsigned char buf[XSYM_FILE_SIZE];
	char target[XSYM_MAX_TARGET + 1];
	size_t tlen = 0;
	int rc;
	enum outcome parsed;

	/* Sequenced deliberately: the order of printf arguments is
	 * unspecified, so filling buf inside the call would leave it
	 * read-before-written. */
	rc = xsym_format("", 0, buf);
	parsed = xsym_parse(buf, target, sizeof(target), &tlen);

	printf("format_rc=%d declared=%.4s digest=%.32s parse=%s\n",
	    rc, buf + XSYM_LEN_OFFSET, buf + XSYM_MD5_OFFSET,
	    outcome_names[parsed]);
	return 0;
}
"""
        run = self.run_probe(body, "xsym_empty_asymmetry")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.strip(),
            "format_rc=0 declared=0000 "
            "digest=d41d8cd98f00b204e9800998ecf8427e parse=empty_target",
        )


class TestPathLogic(SymlinkConverterHarness):
    """Containment decides what the walk may touch, so its edges matter."""

    def test_path_under_respects_component_boundaries(self) -> None:
        body = r"""
static void
show(const char *parent, const char *child)
{
	const char *rel = NULL;
	int under = path_under(parent, child, &rel);

	printf("%s|%s=%d:%s\n", parent, child, under, under ? rel : "");
}

int
main(void)
{
	show("/a/b", "/a/bc");
	show("/a/b", "/a/b");
	show("/a/b", "/a/b/c");
	show("/a/b/", "/a/b/c");
	show("/", "/a");
	show("/a/b", "/x");
	return 0;
}
"""
        run = self.run_probe(body, "path_under_cases")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.split(),
            [
                "/a/b|/a/bc=0:",
                "/a/b|/a/b=1:",
                "/a/b|/a/b/c=1:c",
                "/a/b/|/a/b/c=1:c",
                "/|/a=1:a",
                "/a/b|/x=0:",
            ],
        )

    def test_canonicalize_abs_folds_dot_and_dotdot_textually(self) -> None:
        # Textual, not realpath(): the server decides containment the same way,
        # and resolving symlinks here would answer a different question.
        body = r"""
static void
show(const char *path)
{
	char out[PATH_MAX];

	if (canonicalize_abs(path, out, sizeof(out)) != 0) {
		printf("%s=ERR\n", path);
		return;
	}
	printf("%s=%s\n", path, out);
}

int
main(void)
{
	show("/a/b/../../../x");
	show("/a/./b//c");
	show("/a/..");
	show("/");
	show("/a/b/../c");
	show("relative/path");
	return 0;
}
"""
        run = self.run_probe(body, "canonicalize_cases")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.split(),
            [
                "/a/b/../../../x=/x",
                "/a/./b//c=/a/b/c",
                "/a/..=/",
                "/=/",
                "/a/b/../c=/a/c",
                "relative/path=ERR",
            ],
        )

    def test_exclusions_match_case_insensitively_except_the_temp_prefix(self) -> None:
        # The share is HFS+, where ".Samba4" and ".samba4" are the same
        # directory, so letting a differently-cased Time Machine directory
        # through would be the unsafe direction to be wrong in. The temporary
        # prefix is the one exception: it is matched with strncmp.
        body = r"""
static void
show(const char *name)
{
	printf("%s=%d\n", name, is_excluded_name(name));
}

int
main(void)
{
	show("FOO.SPARSEBUNDLE");
	show(".SAMBA4");
	show(".Com.Apple.TimeMachine.foo");
	show("Backups.BackupDB");
	show("report.pdf");
	show(".fcpcache");
	show(".tcmig.1234.0.tmp");
	show(".TCMIG.1234.0.tmp");
	return 0;
}
"""
        run = self.run_probe(body, "exclusion_cases")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.split(),
            [
                "FOO.SPARSEBUNDLE=1",
                ".SAMBA4=1",
                ".Com.Apple.TimeMachine.foo=1",
                "Backups.BackupDB=1",
                "report.pdf=0",
                # A hidden symlink on the measured share; must not be skipped.
                ".fcpcache=0",
                ".tcmig.1234.0.tmp=1",
                ".TCMIG.1234.0.tmp=0",
            ],
        )


class TestShareCheck(SymlinkConverterHarness):
    """The root must be inside a directory smbd actually serves, so the tool
    cannot be aimed at a system directory or at the payload by mistake."""

    SHARE_PROBE = r"""
int
main(int argc, char **argv)
{
	if (argc != 3)
		return 2;
	printf("%d\n", root_is_inside_a_share(argv[1], argv[2]));
	return 0;
}
"""

    CONF = textwrap.dedent(
        """\
        [global]
        \tworkgroup = WORKGROUP

        [Data]
        \tpath = /Volumes/dk2/ShareRoot/Data
        \tread only = no

        [scratch]
           path   =   /Volumes/dk2/__smbtest__

        [broken]
        \tpath = relative/not/absolute
        """
    )

    def check(self, conf: str | None, root: str) -> str:
        bin_path = self.compile_c(self.SHARE_PROBE, "share_check")
        with tempfile.TemporaryDirectory() as tmp:
            if conf is None:
                conf_path = Path(tmp) / "absent.conf"
            else:
                conf_path = Path(tmp) / "smb.conf"
                conf_path.write_text(conf)
            run = subprocess.run(
                [str(bin_path), str(conf_path), root],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            return run.stdout.strip()

    def test_accepts_a_root_inside_a_configured_share(self) -> None:
        self.assertEqual(self.check(self.CONF, "/Volumes/dk2/ShareRoot/Data/sub"), "1")
        self.assertEqual(self.check(self.CONF, "/Volumes/dk2/ShareRoot/Data"), "1")

    def test_tolerates_arbitrary_whitespace_around_the_assignment(self) -> None:
        self.assertEqual(self.check(self.CONF, "/Volumes/dk2/__smbtest__/x"), "1")

    def test_rejects_a_root_outside_every_share(self) -> None:
        self.assertEqual(self.check(self.CONF, "/etc"), "0")
        # Component boundaries again: DataOther is not inside Data.
        self.assertEqual(self.check(self.CONF, "/Volumes/dk2/ShareRoot/DataOther"), "0")

    def test_ignores_a_share_whose_path_is_not_absolute(self) -> None:
        self.assertEqual(self.check(self.CONF, "relative/not/absolute"), "0")

    def test_distinguishes_an_unreadable_config_from_no_share_matching(self) -> None:
        # -1 rather than 0: main() turns these into different messages, because
        # "cannot read the config" and "not inside a share" need different fixes.
        self.assertEqual(self.check(None, "/Volumes/dk2/ShareRoot/Data"), "-1")


class ConverterBinaryTests(SymlinkConverterHarness):
    """Drives the real main() over a temporary tree.

    Every run is a fresh process. main() resets only opts.mode, so --apply and
    the counters survive into a second call within one process: a dry run after
    a real one would silently write. Reusing a process here would test the
    opposite of what the assertions claim.

    Every run also passes --any-path, because the share check reads
    /mnt/Memory/samba4/etc/smb.conf, which exists only on the device.
    """

    _binary: Path | None = None
    _binary_dir: tempfile.TemporaryDirectory[str] | None = None

    BINARY_PROBE = r"""
int
main(int argc, char **argv)
{
	return symlink_converter_main(argc, argv);
}
"""

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._binary_dir is not None:
            cls._binary_dir.cleanup()
            cls._binary_dir = None
            cls._binary = None

    def converter(self) -> Path:
        # Compiled once for the class: the 1300-line translation unit is the
        # expensive part, not the fork. The binary outlives each test, so it
        # cannot live in a directory a single test cleans up.
        if ConverterBinaryTests._binary is not None:
            return ConverterBinaryTests._binary

        ConverterBinaryTests._binary_dir = tempfile.TemporaryDirectory()
        tmp = Path(ConverterBinaryTests._binary_dir.name)
        source = HARNESS_PROLOGUE.format(converter=CONVERTER.as_posix()) + self.BINARY_PROBE
        c_path = tmp / "symlink_converter_bin.c"
        bin_path = tmp / "symlink_converter_bin"
        c_path.write_text(source)
        proc = subprocess.run(
            ["cc", *self.CFLAGS, str(c_path), "-o", str(bin_path)] + self.MD5_LIBS,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ConverterBinaryTests._binary = bin_path
        return bin_path

    def run_converter(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.converter()), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def tree(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # main() canonicalises --root with realpath(3) and reports paths built
        # from the result, so on macOS an unresolved /var root comes back as
        # /private/var. Resolve here and the reported paths match.
        return tmp.resolve()

    def test_converts_a_posix_symlink_into_the_body_macos_recognises(self) -> None:
        root = self.tree()
        (root / "plain.txt").write_text("hello world")
        os.symlink("plain.txt", root / "link")

        run = self.run_converter(
            "--mode", "posix-to-xsym", "--root", str(root), "--any-path", "--apply"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        written = (root / "link").read_bytes()
        self.assertEqual(len(written), 1067)
        # Against the constant-built fixture rather than xsym_format's output:
        # both would come from this same source, so comparing them would prove
        # only that the binary agrees with itself.
        self.assertEqual(written, GOLDEN_XSYM)
        self.assertFalse((root / "link").is_symlink())
        self.assertIn("converted\t" + str(root / "link") + "\tplain.txt", run.stdout)

    def test_without_apply_nothing_is_written(self) -> None:
        root = self.tree()
        (root / "plain.txt").write_text("hello world")
        os.symlink("plain.txt", root / "link")

        run = self.run_converter(
            "--mode", "posix-to-xsym", "--root", str(root), "--any-path"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("would_convert", run.stdout)
        self.assertTrue((root / "link").is_symlink())
        self.assertEqual(os.readlink(root / "link"), "plain.txt")

    def test_round_trip_returns_the_original_target(self) -> None:
        root = self.tree()
        (root / "plain.txt").write_text("hello world")
        os.symlink("plain.txt", root / "link")

        first = self.run_converter(
            "--mode", "posix-to-xsym", "--root", str(root), "--any-path", "--apply"
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self.run_converter(
            "--mode", "xsym-to-posix", "--root", str(root), "--any-path", "--apply"
        )
        self.assertEqual(second.returncode, 0, second.stderr)

        self.assertTrue((root / "link").is_symlink())
        self.assertEqual(os.readlink(root / "link"), "plain.txt")

    def test_converts_an_xsym_object_written_by_hand(self) -> None:
        root = self.tree()
        (root / "plain.txt").write_text("hello world")
        (root / "link").write_bytes(GOLDEN_XSYM)

        run = self.run_converter(
            "--mode", "xsym-to-posix", "--root", str(root), "--any-path", "--apply"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((root / "link").is_symlink())
        self.assertEqual(os.readlink(root / "link"), "plain.txt")

    def test_a_1067_byte_file_that_is_not_an_xsym_object_is_left_alone(self) -> None:
        # The production share holds 51 of these; converting one would destroy
        # a file that merely happens to be the right size.
        root = self.tree()
        (root / "coincidence").write_bytes(b"A" * 1067)

        run = self.run_converter(
            "--mode", "xsym-to-posix", "--root", str(root), "--any-path", "--apply"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("bad_magic", run.stdout)
        self.assertEqual((root / "coincidence").read_bytes(), b"A" * 1067)
        self.assertFalse((root / "coincidence").is_symlink())

    def test_an_excluded_directory_is_never_entered(self) -> None:
        root = self.tree()
        bundle = root / "Movie.sparsebundle"
        bundle.mkdir()
        (bundle / "plain.txt").write_text("x")
        os.symlink("plain.txt", bundle / "link")

        run = self.run_converter(
            "--mode", "posix-to-xsym", "--root", str(root), "--any-path", "--apply"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((bundle / "link").is_symlink())
        self.assertNotIn("converted", run.stdout)

    def test_a_long_target_still_converts_when_it_fits(self) -> None:
        # A symlink target cannot reach the format's 1024-byte limit here:
        # PATH_MAX is 1024 and symlink(2) refuses anything at or above it with
        # ENAMETOOLONG, so the two OUT_TARGET_TOO_LONG guards that follow
        # readlink(2) are out of reach from an ordinary filesystem. The
        # 1024/1025 boundary is covered by calling xsym_format directly in
        # TestXsymFormat. This case keeps the long-but-valid path honest, using
        # an absolute target so the length does not depend on where the
        # temporary directory happens to live.
        root = self.tree()
        target = "/" + "x" * 900
        os.symlink(target, root / "link")

        run = self.run_converter(
            "--mode", "posix-to-xsym", "--root", str(root), "--any-path", "--apply"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("converted", run.stdout)
        self.assertEqual(len((root / "link").read_bytes()), 1067)

    def test_a_relative_target_that_overflows_the_joined_path_is_refused(self) -> None:
        # A third OUT_TARGET_TOO_LONG, distinct from the two after readlink(2):
        # a relative target is joined onto the link's own directory before the
        # containment test, and that join has to fit PATH_MAX. So whether a
        # given target is too long depends on how deep the link sits, not on
        # the target alone.
        root = self.tree()
        target = "x" * 1000
        os.symlink(target, root / "link")

        run = self.run_converter(
            "--mode", "posix-to-xsym", "--root", str(root), "--any-path", "--apply"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("target_too_long\t" + str(root / "link"), run.stdout)
        self.assertTrue((root / "link").is_symlink())

    def test_usage_errors_exit_two(self) -> None:
        root = self.tree()
        cases = {
            "unknown mode": ("--mode", "sideways", "--root", str(root), "--any-path"),
            "inventory with apply": (
                "--mode", "inventory", "--root", str(root), "--any-path", "--apply",
            ),
            "root is the filesystem root": ("--mode", "inventory", "--root", "/", "--any-path"),
            "unknown argument": (
                "--mode", "inventory", "--root", str(root), "--any-path", "--nonsense",
            ),
        }
        for label, args in cases.items():
            with self.subTest(label):
                run = self.run_converter(*args)
                self.assertEqual(run.returncode, 2, f"{label}: {run.stderr}")

    def test_a_dangling_mode_flag_reports_it_as_an_unknown_argument(self) -> None:
        # "--mode" with nothing after it fails the "i + 1 < argc" guard and
        # falls through to the unknown-argument branch, so the message names
        # --mode itself rather than the missing value. Confusing for an
        # operator, but it is what the tool does.
        run = self.run_converter("--mode")

        self.assertEqual(run.returncode, 2)
        self.assertIn("unknown argument: --mode", run.stderr)

    def test_a_root_that_is_not_a_directory_is_refused_by_name(self) -> None:
        # Asserting the message, not just the code: without --any-path this
        # would also exit 2, but from the share check, and the test could not
        # tell the two apart.
        root = self.tree()
        plain = root / "plain.txt"
        plain.write_text("x")

        run = self.run_converter("--mode", "inventory", "--root", str(plain), "--any-path")

        self.assertEqual(run.returncode, 2)
        self.assertIn("not a directory", run.stderr)

    def test_a_missing_root_argument_prints_the_usage_and_exits_two(self) -> None:
        run = self.run_converter("--mode", "inventory", "--any-path")

        self.assertEqual(run.returncode, 2)
        self.assertIn("--root <path>      Directory to walk; required", run.stderr)

    def test_a_relative_root_is_accepted_and_resolved(self) -> None:
        # Unlike --only, which must be absolute, --root goes through
        # realpath(3) and so may be given relative to the working directory.
        # The reported paths come back absolute either way.
        root = self.tree()
        (root / "plain.txt").write_text("hello world")
        os.symlink("plain.txt", root / "link")

        run = subprocess.run(
            [str(self.converter()), "--mode", "inventory", "--root", ".", "--any-path"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("already\t" + str(root / "link") + "\tplain.txt", run.stdout)

    def test_a_root_reached_through_a_symlink_is_resolved_before_the_walk(self) -> None:
        # realpath(3) again: the walk reports the resolved path, not the one
        # given. Worth pinning because every reported path, and the --only
        # containment test, is built from the resolved root.
        root = self.tree()
        real = root / "real"
        real.mkdir()
        (real / "plain.txt").write_text("hello world")
        os.symlink("plain.txt", real / "link")
        os.symlink("real", root / "viasym")

        run = self.run_converter(
            "--mode", "inventory", "--root", str(root / "viasym"), "--any-path"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("already\t" + str(real / "link") + "\tplain.txt", run.stdout)
        self.assertNotIn("viasym", run.stdout)

    def test_the_share_check_reads_the_config_given_with_config(self) -> None:
        # The same three-way answer TestShareCheck pins at the function level,
        # but driven through the binary, so the exit codes main() derives from
        # it are covered too.
        root = self.tree()
        shared = root / "shared"
        shared.mkdir()
        outside = root / "outside"
        outside.mkdir()
        conf = root / "smb.conf"
        conf.write_text("[scratch]\n\tpath = {}\n".format(shared))

        inside = self.run_converter(
            "--mode", "inventory", "--root", str(shared), "--config", str(conf)
        )
        self.assertEqual(inside.returncode, 0, inside.stderr)

        elsewhere = self.run_converter(
            "--mode", "inventory", "--root", str(outside), "--config", str(conf)
        )
        self.assertEqual(elsewhere.returncode, 2)
        self.assertIn("is not inside any share", elsewhere.stderr)

        absent = self.run_converter(
            "--mode", "inventory", "--root", str(shared), "--config", str(root / "absent.conf")
        )
        self.assertEqual(absent.returncode, 2)
        self.assertIn("cannot read", absent.stderr)

    def test_a_missing_root_exits_one_rather_than_two(self) -> None:
        # realpath(3) fails, which is a runtime failure rather than a usage
        # error -- the operator's arguments were well formed.
        root = self.tree()

        run = self.run_converter(
            "--mode", "inventory", "--root", str(root / "absent"), "--any-path"
        )

        self.assertEqual(run.returncode, 1)

    def test_without_any_path_the_share_check_refuses_an_unreadable_config(self) -> None:
        root = self.tree()

        run = self.run_converter("--mode", "inventory", "--root", str(root))

        self.assertEqual(run.returncode, 2)
        self.assertIn("cannot read", run.stderr)

    def test_help_exits_zero(self) -> None:
        run = self.run_converter("--help")

        self.assertEqual(run.returncode, 0)
        self.assertIn("Usage: symlink-converter", run.stderr)

    def test_a_directory_that_cannot_be_written_is_reported_and_fails_the_run(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores the write permission bit")
        root = self.tree()
        sub = root / "locked"
        sub.mkdir()
        (sub / "plain.txt").write_text("x")
        os.symlink("plain.txt", sub / "link")
        sub.chmod(0o500)
        self.addCleanup(sub.chmod, 0o700)

        run = self.run_converter(
            "--mode", "posix-to-xsym", "--root", str(root), "--any-path", "--apply"
        )

        self.assertEqual(run.returncode, 1)
        # Through record(), so it is a tab-separated line on stdout naming the
        # object -- unlike the opendir failure below.
        self.assertIn("dir_not_writable\t" + str(sub / "link"), run.stdout)
        self.assertTrue((sub / "link").is_symlink())

    def test_a_directory_that_cannot_be_read_is_named_on_stderr_and_fails_the_run(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores the read permission bit")
        root = self.tree()
        sub = root / "unreadable"
        sub.mkdir()
        sub.chmod(0o000)
        self.addCleanup(sub.chmod, 0o700)

        run = self.run_converter(
            "--mode", "inventory", "--root", str(root), "--any-path"
        )

        self.assertEqual(run.returncode, 1)
        # This one never reaches record(): walk() bumps the counter itself and
        # reports through warn_errno, so the object is named on stderr and
        # stdout carries no line for it at all.
        self.assertIn(str(sub), run.stderr)
        self.assertNotIn("permission_denied\t", run.stdout)

    def test_a_hardlinked_xsym_object_is_refused_only_when_writing(self) -> None:
        # rename(2) replaces one directory entry, so the other names would keep
        # resolving to the original 1067-byte file. An inventory can still
        # report it, and reports the target, so it answers rather than refuses.
        def build() -> Path:
            root = self.tree()
            (root / "plain.txt").write_text("hello world")
            (root / "one").write_bytes(GOLDEN_XSYM)
            os.link(root / "one", root / "two")
            return root

        root = build()
        applied = self.run_converter(
            "--mode", "xsym-to-posix", "--root", str(root), "--any-path", "--apply"
        )
        self.assertEqual(applied.returncode, 1)
        self.assertIn("hardlinked\t", applied.stdout)
        self.assertFalse((root / "one").is_symlink())

        root = build()
        dry = self.run_converter(
            "--mode", "xsym-to-posix", "--root", str(root), "--any-path"
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("hardlinked\t", dry.stdout)

        root = build()
        survey = self.run_converter(
            "--mode", "inventory", "--root", str(root), "--any-path"
        )
        self.assertEqual(survey.returncode, 0, survey.stderr)
        self.assertIn("already\t", survey.stdout)
        self.assertNotIn("hardlinked\t", survey.stdout)
        self.assertIn("hardlinked:", survey.stderr)

    def test_every_refusal_names_the_object_and_its_reason_on_one_line(self) -> None:
        root = self.tree()
        (root / "damaged").write_bytes(GOLDEN_XSYM[:10] + b"f" * 32 + GOLDEN_XSYM[42:])
        (root / "coincidence").write_bytes(b"A" * 1067)

        run = self.run_converter(
            "--mode", "xsym-to-posix", "--root", str(root), "--any-path", "--apply"
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        reported = {}
        for line in run.stdout.splitlines():
            fields = line.split("\t")
            self.assertGreaterEqual(len(fields), 2, line)
            reported[fields[1]] = fields[0]
        self.assertEqual(reported[str(root / "damaged")], "bad_md5")
        self.assertEqual(reported[str(root / "coincidence")], "bad_magic")
