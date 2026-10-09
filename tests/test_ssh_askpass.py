"""Run the shipped password helper against each prompt category."""
import os

import pytest

from timecapsulesmb.core.process import run_process
from timecapsulesmb.transport.ssh import SSH_ASKPASS_PATH


@pytest.mark.parametrize("prompt,password,status,output", [
    ("Are you sure you want to continue connecting (yes/no)?", "secret", 0, "yes\n"),
    ("(root@h) Password:", "secret", 0, "secret\n"),
    ("root@h's password:", "-a\\b%é$(id)", 0, "-a\\b%é$(id)\n"),
    ("Enter passphrase for key '/tmp/key':", "secret", 1, ""),
    ("Enter passphrase for key '/tmp/password-key':", "secret", 1, ""),
    ("Enter root's new password:", "secret", 1, ""),
    ("New password:", "secret", 1, ""),
    ("Retype new password:", "secret", 1, ""),
    ("Verification code:", "secret", 1, ""),
    ("Password:", None, 1, ""),
    ("Password:", "", 1, ""),
])
def test_prompt(prompt, password, status, output):
    env = {k: v for k, v in os.environ.items() if k != "TCAPSULE_SSH_PASSWORD"}
    if password is not None:
        env["TCAPSULE_SSH_PASSWORD"] = password
    result = run_process([str(SSH_ASKPASS_PATH), prompt], env=env, capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (status, output, "")
