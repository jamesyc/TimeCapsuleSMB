"""Let ssh accept the Time Capsule's SHA-1 signature under Red Hat's OpenSSL.

Fedora, RHEL and their derivatives patch OpenSSL to refuse SHA-1 signatures
unless its config sets `rh-allow-sha1-signatures = yes`. Fedora's crypto
policy writes `= no`; RHEL 9 and 10 and their rebuilds leave the key out and
refuse them all the same. The Time Capsule's SSH server signs the key exchange
only with ssh-rsa, a SHA-1 signature, so ssh then stops before logging in with
"ssh_dispatch_run_fatal: ... error in libcrypto". No ssh option reaches that
setting; an OpenSSL config does.

openssl-sha1.cnf loads the config OpenSSL would otherwise read and allows
SHA-1 signatures on top of it, so the rest of the policy (providers, FIPS)
still applies. Only ssh children get it, through OPENSSL_CONF. Some of the
family allow SHA-1 without the key (Amazon Linux); the config changes nothing
there. Accepting SHA-1 gives nothing away: ssh already skips host key
verification for the device (StrictHostKeyChecking=no,
UserKnownHostsFile=/dev/null).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path

from timecapsulesmb.core.os_release import OS_RELEASE_PATH, parse_os_release


SHA1_OPENSSL_CONF_PATH = Path(__file__).with_name("openssl-sha1.cnf")
# The config openssl-sha1.cnf includes, named by this variable.
BASE_OPENSSL_CONF_ENV = "TCAPSULE_OPENSSL_BASE_CONF"
# The OpenSSL settings of the system crypto policy (crypto-policies).
CRYPTO_POLICY_OPENSSL_PATH = Path("/etc/crypto-policies/back-ends/opensslcnf.config")
# The config OpenSSL reads without OPENSSL_CONF on Red Hat family systems.
SYSTEM_OPENSSL_CONF_PATH = Path("/etc/pki/tls/openssl.cnf")
# os-release ID or ID_LIKE of a distribution that ships Red Hat's OpenSSL.
RED_HAT_FAMILY_IDS = frozenset({"fedora", "rhel"})
# sha1_signature_policy's answers.
SHA1_OVERRIDE = "override"
SHA1_REFUSED = "refused"
_SHA1_SIGNATURES_SETTING = re.compile(r"^\s*rh-allow-sha1-signatures\s*=\s*(\w+)\s*$", re.MULTILINE)


def crypto_policy_sha1_signatures(policy_path: Path | None = None) -> str | None:
    """The policy's rh-allow-sha1-signatures value ("yes" or "no"), or None without one."""
    try:
        text = (policy_path or CRYPTO_POLICY_OPENSSL_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _SHA1_SIGNATURES_SETTING.search(text)
    return match.group(1).lower() if match else None


def red_hat_family(os_release_path: Path | None = None) -> bool:
    values = parse_os_release(os_release_path or OS_RELEASE_PATH)
    ids = {values.get("ID", ""), *values.get("ID_LIKE", "").split()}
    return not ids.isdisjoint(RED_HAT_FAMILY_IDS)


def openssl_refuses_sha1_signatures(
    *,
    platform: str | None = None,
    os_release_path: Path | None = None,
    policy_path: Path | None = None,
) -> bool:
    """Whether this computer's OpenSSL may refuse SHA-1 signatures.

    A policy that says no means Red Hat's OpenSSL whatever the distribution
    calls itself; one that says yes allows them. Without the key, Red Hat's
    OpenSSL refuses them.
    """
    if not (platform or sys.platform).startswith("linux"):
        return False
    setting = crypto_policy_sha1_signatures(policy_path)
    if setting is not None:
        return setting == "no"
    return red_hat_family(os_release_path)


def sha1_signature_env(
    environ: Mapping[str, str],
    *,
    platform: str | None = None,
    os_release_path: Path | None = None,
    policy_path: Path | None = None,
    system_conf_path: Path | None = None,
) -> dict[str, str]:
    """The variables that let an ssh child accept SHA-1 signatures, or none.

    None unless this is Linux whose OpenSSL may refuse them. The included
    config is the user's own OPENSSL_CONF, else the system's; when it is
    missing, OpenSSL could not load openssl-sha1.cnf either, so nothing changes.
    The override takes effect only when the included config names
    evp_properties as its alg_section, as Red Hat's system config does.
    """
    if not openssl_refuses_sha1_signatures(platform=platform, os_release_path=os_release_path, policy_path=policy_path):
        return {}
    base = environ.get("OPENSSL_CONF") or str(system_conf_path or SYSTEM_OPENSSL_CONF_PATH)
    if Path(base) == SHA1_OPENSSL_CONF_PATH:
        # Already a child of ours: include what that child included.
        base = environ.get(BASE_OPENSSL_CONF_ENV, "")
    if not base or not Path(base).is_file():
        return {}
    return {"OPENSSL_CONF": str(SHA1_OPENSSL_CONF_PATH), BASE_OPENSSL_CONF_ENV: base}


def sha1_signature_policy(environ: Mapping[str, str], *, platform: str | None = None) -> str | None:
    """For telemetry: SHA1_OVERRIDE when ssh gets openssl-sha1.cnf,
    SHA1_REFUSED when OpenSSL may refuse SHA-1 signatures and the override
    cannot apply, None when it allows them."""
    if sha1_signature_env(environ, platform=platform):
        return SHA1_OVERRIDE
    if openssl_refuses_sha1_signatures(platform=platform):
        return SHA1_REFUSED
    return None
