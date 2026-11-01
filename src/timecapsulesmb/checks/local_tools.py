from __future__ import annotations

import os
from pathlib import Path

from timecapsulesmb.checks import smb as local_smb
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.install_validation import validate_ssh_askpass
from timecapsulesmb.transport.ssh_client import require_local_ssh
from timecapsulesmb.transport.errors import SshError
from timecapsulesmb.transport.openssl_sha1 import SHA1_OPENSSL_CONF_PATH, sha1_signature_env


def check_required_local_tools() -> list[CheckResult]:
    results: list[CheckResult] = []
    # Every device command runs the system ssh; require_local_ssh finds it and
    # checks its version, or says why it cannot be used.
    try:
        require_local_ssh()
    except SshError as exc:
        results.append(CheckResult("FAIL", str(exc)))
    else:
        results.append(CheckResult("PASS", "local tool ssh is installed"))
    smb = local_smb.local_smb_client()
    if smb.path is None:
        results.append(CheckResult("FAIL", smb.missing_message()))
    else:
        results.append(CheckResult("PASS", f"local tool {smb.tool} is installed"))
    # ssh gets the device password from this helper, installed with TimeCapsuleSMB.
    askpass = validate_ssh_askpass()
    results.append(CheckResult("PASS" if askpass.ok else "FAIL", askpass.message))
    # This computer's OpenSSL may refuse the Time Capsule's SHA-1 signature;
    # ssh gets this config to allow it (transport.openssl_sha1).
    if sha1_signature_env(os.environ):
        if SHA1_OPENSSL_CONF_PATH.is_file():
            results.append(CheckResult(
                "PASS",
                "found the OpenSSL config that lets SSH accept the Time Capsule's SHA-1 signature",
            ))
        else:
            results.append(CheckResult(
                "FAIL",
                "the OpenSSL config that lets SSH accept the Time Capsule's SHA-1 signature is missing at "
                f"{SHA1_OPENSSL_CONF_PATH}; reinstall TimeCapsuleSMB",
            ))
    return results


def check_required_artifacts(repo_root: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for _, ok, message in validate_artifacts(repo_root):
        if ok:
            results.append(CheckResult("PASS", message))
        else:
            results.append(CheckResult("FAIL", message))
    return results
