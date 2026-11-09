"""The native manager's disk-owner handoff, read from its process title.

The manager publishes `diskd=PHASE:SINCE:REASON` (build/native/README.md,
"Disk owner handoff"). SINCE is the device's monotonic second when the phase
or the reason last changed. Managers before v3.8.5 publish no such word.

Phases: inspect, quiesce, stopping, launching, starting, ready. Reasons, each
claiming only this much:
- none: nothing holds the phase back.
- maintenance: a disk tool, or a shell a diskd started, runs.
- residual_mounts: no diskd runs and an HFS data volume is still mounted.
- conflict: two or more independent diskd run.
- no_answer: the last two readiness probes in a row got no answer. acp
  failed (any error, -6727 "not registered" included), ran past its limit or
  was killed, or the manager could not start the probe; the manager's log
  says which.
- relaunch_wait: no diskd runs and the next launch waits out its backoff
  (the last launch exited before exec, or the manager's diskd exited).
- observation: the manager could not tell this pass.
- busy: Apple's diskd is not idle in its event loop, or an acp client runs,
  so its TERM waits: one sent before then can deadlock it with ACPd
  (build/native/README.md, F19).
"""
from __future__ import annotations

from dataclasses import dataclass

from timecapsulesmb.core.error_codes import ErrorCode

# What only a restart clears, once it has held this long with no disk work.
RESTART_REASONS = {
    "conflict": "Two copies of Apple's disk service are running.",
    "no_answer": ("Apple's disk service has not answered the manager's requests: each one failed, timed out, "
                  "or could not be sent."),
    "residual_mounts": "A volume is still mounted from a disk service that is no longer running.",
    "busy": "Apple's own disk service never became idle, so it could not be asked to hand over the disks.",
}
# Apple's own diskd that has not exited since its TERM, with no disk tool
# running to explain it: diskd can hang on a disk that stopped answering.
STOPPING_CAUSE = "Apple's own disk service did not stop when asked."
RESTART_AFTER_SECONDS = 600
# Every state short of ready that has not held long enough for a restart:
# a disk check, a conflict, no answer yet, leftover mounts. Naming one of
# them would understate the others; the detail has the exact state.
PENDING_MESSAGE = "Apple's disk service is still preparing file sharing."
RESTART_ADVICE = "After backups and disk operations finish, restart the device, then run \"Checkup\" again."


@dataclass(frozen=True)
class DiskdHandoff:
    phase: str
    since: int
    reason: str

    @property
    def ready(self) -> bool:
        return self.phase == "ready"

    @property
    def detail(self) -> str:
        return f"diskd phase={self.phase} reason={self.reason} since={self.since}"

    def verdict(self, now_monotonic_ms: int | None) -> tuple[ErrorCode, str] | None:
        """Why file sharing waits on the handoff: None once it is ready. A
        phase or reason this release does not know is only ever pending."""
        if self.ready:
            return None
        cause = RESTART_REASONS.get(self.reason)
        if self.phase == "stopping" and self.reason == "none":
            cause = STOPPING_CAUSE
        if (cause is not None and now_monotonic_ms is not None
                and now_monotonic_ms // 1000 - self.since >= RESTART_AFTER_SECONDS):
            return ErrorCode.DISKD_RESTART_NEEDED, f"{cause} {RESTART_ADVICE}"
        return ErrorCode.DISKD_HANDOFF_PENDING, PENDING_MESSAGE


def parse_diskd_handoff(title: list[str] | tuple[str, ...]) -> DiskdHandoff | None:
    """The title's diskd=PHASE:SINCE:REASON word. None when there is no such
    word (a manager before v3.8.5) or it does not have that shape: callers
    then say nothing about the handoff and fall back to their own error."""
    for word in title:
        if word.startswith("diskd="):
            parts = word.removeprefix("diskd=").split(":")
            if len(parts) == 3 and parts[1].isascii() and parts[1].isdecimal():
                return DiskdHandoff(parts[0], int(parts[1]), parts[2])
            return None
    return None
