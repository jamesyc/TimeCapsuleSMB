"""TimeCapsuleSMB package."""
import sys

# Every way in imports this package first: the tcapsule command, ./tcapsule,
# python -m, and a command or service module imported directly. So native
# Windows is turned away here, before any module of ours loads or does
# anything. Only win32: Cygwin, MSYS and other POSIX Pythons are left to try.
if sys.platform == "win32":
    print("TimeCapsuleSMB supports only macOS and Linux. Run this command on a Mac or Linux computer.", file=sys.stderr)
    raise SystemExit(1)
