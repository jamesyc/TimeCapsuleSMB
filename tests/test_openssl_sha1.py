from __future__ import annotations

import tempfile
import unittest
from importlib import resources
from pathlib import Path
from unittest import mock

from timecapsulesmb.transport import openssl_sha1
from timecapsulesmb.transport.openssl_sha1 import (
    BASE_OPENSSL_CONF_ENV,
    SHA1_OPENSSL_CONF_PATH,
    SHA1_OVERRIDE,
    SHA1_REFUSED,
    crypto_policy_sha1_signatures,
    openssl_refuses_sha1_signatures,
    sha1_signature_env,
    sha1_signature_policy,
)


# What Fedora's DEFAULT crypto policy writes for OpenSSL.
FEDORA_DEFAULT_POLICY = """CipherString = @SECLEVEL=2:kEECDH:kRSA
TLS.MinProtocol = TLSv1.2

[openssl_init]
alg_section = evp_properties

[evp_properties]
rh-allow-sha1-signatures = no
"""
# The same policy with the SHA1 subpolicy (update-crypto-policies --set DEFAULT:SHA1).
FEDORA_SHA1_POLICY = FEDORA_DEFAULT_POLICY.replace("= no", "= yes")
# RHEL 9 and 10 and their rebuilds write no key, and their OpenSSL refuses SHA-1.
RHEL_DEFAULT_POLICY = "CipherString = @SECLEVEL=2:kEECDH:kRSA\nTLS.MinProtocol = TLSv1.2\n"

# os-release of the distributions the rule was checked on.
FEDORA = 'NAME="Fedora Linux"\nID=fedora\nVERSION_ID=44\n'
ALMALINUX = 'NAME="AlmaLinux"\nID="almalinux"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.8"\n'
CENTOS_STREAM = 'NAME="CentOS Stream"\nID="centos"\nID_LIKE="rhel fedora"\nVERSION_ID="10"\n'
AMAZON_LINUX = 'NAME="Amazon Linux"\nID="amzn"\nID_LIKE="fedora"\nVERSION_ID="2023"\n'
DEBIAN = 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_ID="13"\n'
UBUNTU = 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'


class OpensslSha1Tests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.os_release = self.root / "os-release"
        self.policy = self.root / "opensslcnf.config"
        self.system_conf = self.root / "openssl.cnf"
        self.system_conf.write_text("openssl_conf = openssl_init\n")

    def write(self, path: Path, text: str | None) -> None:
        path.unlink(missing_ok=True)
        if text is not None:
            path.write_text(text)

    def env(
        self,
        environ: dict[str, str] | None = None,
        *,
        platform: str = "linux",
        os_release: str | None = FEDORA,
        policy: str | None = FEDORA_DEFAULT_POLICY,
    ) -> dict[str, str]:
        self.write(self.os_release, os_release)
        self.write(self.policy, policy)
        return sha1_signature_env(
            environ or {},
            platform=platform,
            os_release_path=self.os_release,
            policy_path=self.policy,
            system_conf_path=self.system_conf,
        )

    def test_red_hat_openssl_that_may_refuse_sha1_includes_the_system_config(self) -> None:
        expected = {"OPENSSL_CONF": str(SHA1_OPENSSL_CONF_PATH), BASE_OPENSSL_CONF_ENV: str(self.system_conf)}
        cases = {
            "Fedora, policy says no": dict(os_release=FEDORA, policy=FEDORA_DEFAULT_POLICY),
            "AlmaLinux, no key": dict(os_release=ALMALINUX, policy=RHEL_DEFAULT_POLICY),
            "CentOS Stream, no key": dict(os_release=CENTOS_STREAM, policy=RHEL_DEFAULT_POLICY),
            # Allows SHA-1 without the key; the config changes nothing there.
            "Amazon Linux, no key": dict(os_release=AMAZON_LINUX, policy=RHEL_DEFAULT_POLICY),
            "Red Hat family without crypto-policies": dict(os_release=ALMALINUX, policy=None),
            # Only Red Hat's OpenSSL reads the key, whatever os-release says.
            "policy says no, unknown distribution": dict(os_release=None, policy=FEDORA_DEFAULT_POLICY),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.assertEqual(self.env(**kwargs), expected)

    def test_nothing_changes_where_sha1_signatures_are_allowed(self) -> None:
        cases = {
            "Fedora with the SHA1 subpolicy": dict(os_release=FEDORA, policy=FEDORA_SHA1_POLICY),
            "AlmaLinux with the SHA1 subpolicy": dict(os_release=ALMALINUX, policy=FEDORA_SHA1_POLICY),
            "Debian": dict(os_release=DEBIAN, policy=None),
            "Ubuntu": dict(os_release=UBUNTU, policy=None),
            # Another distribution's crypto-policies without Red Hat's key.
            "Debian with crypto-policies": dict(os_release=DEBIAN, policy=RHEL_DEFAULT_POLICY),
            "no os-release": dict(os_release=None, policy=None),
            "macOS": dict(platform="darwin", os_release=None, policy=FEDORA_DEFAULT_POLICY),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.assertEqual(self.env(**kwargs), {})

    def test_the_users_own_openssl_conf_is_included_instead_of_the_systems(self) -> None:
        own = self.root / "my openssl.cnf"
        own.write_text("openssl_conf = mine\n")

        env = self.env({"OPENSSL_CONF": str(own)})

        self.assertEqual(env[BASE_OPENSSL_CONF_ENV], str(own))
        self.assertEqual(env["OPENSSL_CONF"], str(SHA1_OPENSSL_CONF_PATH))

    def test_a_missing_base_config_changes_nothing(self) -> None:
        # OpenSSL could not load openssl-sha1.cnf either: ssh would fail the
        # same way, and the classified error then says what to do.
        with self.subTest("system config missing"):
            self.system_conf.unlink()
            self.assertEqual(self.env(), {})
        with self.subTest("user config missing"):
            self.assertEqual(self.env({"OPENSSL_CONF": str(self.root / "gone.cnf")}), {})

    def test_a_child_of_ours_keeps_including_the_config_it_was_given(self) -> None:
        # A process started with our variables would otherwise include
        # openssl-sha1.cnf in itself.
        own = self.root / "base.cnf"
        own.write_text("openssl_conf = mine\n")
        inherited = {"OPENSSL_CONF": str(SHA1_OPENSSL_CONF_PATH), BASE_OPENSSL_CONF_ENV: str(own)}

        self.assertEqual(self.env(inherited)[BASE_OPENSSL_CONF_ENV], str(own))
        self.assertEqual(self.env({"OPENSSL_CONF": str(SHA1_OPENSSL_CONF_PATH)}), {})

    def test_the_policy_setting_is_read_from_its_own_line(self) -> None:
        cases = (
            ("rh-allow-sha1-signatures=no\n", "no"),
            ("  rh-allow-sha1-signatures =  YES  \n", "yes"),
            ("# rh-allow-sha1-signatures = no\nrh-allow-sha1-signatures = yes\n", "yes"),
            ("# rh-allow-sha1-signatures = no\n", None),
            ("CipherString = DEFAULT\n", None),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.policy.write_text(text)
                self.assertEqual(crypto_policy_sha1_signatures(self.policy), expected)
        self.assertIsNone(crypto_policy_sha1_signatures(self.root / "missing"))

    def test_the_default_paths_are_read_when_called(self) -> None:
        # Tests replace the module's paths to keep the host's own distribution
        # and policy out (tests/conftest.py); that only works if they are not
        # bound earlier.
        self.write(self.os_release, ALMALINUX)
        with mock.patch.object(openssl_sha1, "OS_RELEASE_PATH", self.os_release), \
             mock.patch.object(openssl_sha1, "SYSTEM_OPENSSL_CONF_PATH", self.system_conf):
            self.assertTrue(openssl_refuses_sha1_signatures(platform="linux"))
            self.assertEqual(sha1_signature_env({}, platform="linux")[BASE_OPENSSL_CONF_ENV], str(self.system_conf))
        self.write(self.policy, FEDORA_SHA1_POLICY)
        with mock.patch.object(openssl_sha1, "OS_RELEASE_PATH", self.os_release), \
             mock.patch.object(openssl_sha1, "CRYPTO_POLICY_OPENSSL_PATH", self.policy):
            self.assertFalse(openssl_refuses_sha1_signatures(platform="linux"))

    def test_the_policy_state_tells_an_applied_override_from_one_that_cannot_apply(self) -> None:
        cases = (
            # (name, os-release, system config present, platform, expected)
            ("override", ALMALINUX, True, "linux", SHA1_OVERRIDE),
            ("no config to include", ALMALINUX, False, "linux", SHA1_REFUSED),
            ("Debian", DEBIAN, True, "linux", None),
            ("macOS", ALMALINUX, True, "darwin", None),
        )
        for name, os_release, has_system_conf, platform, expected in cases:
            with self.subTest(name):
                self.write(self.os_release, os_release)
                system_conf = self.system_conf if has_system_conf else self.root / "gone.cnf"
                with mock.patch.object(openssl_sha1, "OS_RELEASE_PATH", self.os_release), \
                     mock.patch.object(openssl_sha1, "SYSTEM_OPENSSL_CONF_PATH", system_conf):
                    self.assertEqual(sha1_signature_policy({}, platform=platform), expected)

    def test_the_config_ships_with_the_package_and_overrides_only_the_sha1_key(self) -> None:
        packaged = resources.files("timecapsulesmb.transport").joinpath("openssl-sha1.cnf")
        self.assertTrue(packaged.is_file())
        self.assertEqual(Path(str(packaged)), openssl_sha1.SHA1_OPENSSL_CONF_PATH)
        # The variable the code sets is the one the config includes.
        self.assertIn(f".include $ENV::{BASE_OPENSSL_CONF_ENV}", packaged.read_text())


if __name__ == "__main__":
    unittest.main()
