from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping, get_args

from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.disk_errors import (
    DiskFailingCause,
    DiskFailingEvidence,
    disk_failing_fields,
    disk_failing_variant,
    disk_failing_warning,
)
from timecapsulesmb.services.app import AppOperationError, jsonable


CONFIRMATION_SCHEMA_VERSION = 1
_CONFIRMATION_ONLY_KEYS = frozenset({
    "confirmation_id",
    "confirmation",
})
_SECRET_PARAM_KEYS = frozenset({"password", "credentials"})
# What the app sees about itself, sent with every request for telemetry
# (HelperRunner). It describes the Mac when the request was sent, not what is
# confirmed, and the app works it out again for the confirmed request.
APP_FACT_PARAM_KEYS = frozenset({"macos_app_just_updated", "macos_app_other_copies"})


@dataclass(frozen=True)
class ConfirmationRequest:
    operation: str
    title: str
    message: str
    action_title: str
    risk: str
    confirmation_id: str
    summary: str
    context: Mapping[str, object]
    presentation_id: str
    presentation_values: Mapping[str, object]

    def to_jsonable(self) -> dict[str, object]:
        return {
            "schema_version": CONFIRMATION_SCHEMA_VERSION,
            "operation": self.operation,
            "title": self.title,
            "message": self.message,
            "action_title": self.action_title,
            "risk": self.risk,
            "confirmation_id": self.confirmation_id,
            "summary": self.summary,
            "context": jsonable(dict(self.context)),
            "presentation_id": self.presentation_id,
            "presentation_values": jsonable(dict(self.presentation_values)),
        }


class AppConfirmationRequired(AppOperationError):
    def __init__(self, confirmation: ConfirmationRequest) -> None:
        super().__init__(confirmation.message, code=ErrorCode.CONFIRMATION_REQUIRED)
        self.confirmation = confirmation


def _safe_params(params: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): value
        for key, value in params.items()
        if str(key) not in _CONFIRMATION_ONLY_KEYS | _SECRET_PARAM_KEYS | APP_FACT_PARAM_KEYS
    }


def _confirmation_id(operation: str, params: Mapping[str, object], context: Mapping[str, object]) -> str:
    canonical = {
        "schema_version": CONFIRMATION_SCHEMA_VERSION,
        "operation": operation,
        "params": jsonable(_safe_params(params)),
        "context": jsonable(dict(context)),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_confirmation(
    *,
    operation: str,
    params: Mapping[str, object],
    title: str,
    message: str,
    action_title: str,
    risk: str,
    summary: str,
    context: Mapping[str, object],
    presentation_id: str,
    presentation_values: Mapping[str, object] | None = None,
) -> ConfirmationRequest:
    return ConfirmationRequest(
        operation=operation,
        title=title,
        message=message,
        action_title=action_title,
        risk=risk,
        confirmation_id=_confirmation_id(operation, params, context),
        summary=summary,
        context=context,
        presentation_id=presentation_id,
        presentation_values=presentation_values or {},
    )


# The preflight before an operation that may not fix a failing disk: its
# confirmation says so instead, and the app makes Cancel its default.
_DISK_FAILING_KINDS = {"deploy": ("install", "Install Anyway", "Install anyway?"),
                       "uninstall": ("uninstall", "Uninstall Anyway", "Uninstall anyway?"),
                       "fsck": ("repair", "Repair Anyway", "Repair anyway?"),
                       "wipe-disk": ("wipe", "Wipe Anyway", "Wipe anyway?")}


_VARIANT_SUFFIXES = {"wipe": "_wipe", "usb": "_usb", "plain": ""}


def disk_failing_presentation_id(operation: str, evidence: DiskFailingEvidence) -> str:
    """The app confirmation for this warning: one per cause and wording
    (disk_errors.disk_failing_variant), as the app shows a fixed translation
    for each presentation id."""
    kind = _DISK_FAILING_KINDS[operation][0]
    return f"{operation}.{evidence.cause}{_VARIANT_SUFFIXES[disk_failing_variant(evidence, kind)]}"


# Every failing-disk confirmation the helper can send. Each needs a title,
# message and action in every app language, and the app makes Cancel their
# default (PendingConfirmation.prefersCancel).
DISK_FAILING_PRESENTATION_IDS = (
    *(f"{operation}.{suffix}" for operation in ("deploy", "uninstall", "fsck")
      for suffix in ("disk_failing", "disk_failing_wipe", "disk_failing_usb", "disk_unreadable")),
    "wipe-disk.disk_failing",
    "wipe-disk.disk_unreadable",
)


def disk_failing_confirmation_fields(operation: str, evidence: DiskFailingEvidence) -> dict[str, str]:
    """build_confirmation's title, message, action_title and presentation_id
    for what says the disk may be failing (device.disk_errors.read_disk_failing_evidence_conn)."""
    kind, action_title, question = _DISK_FAILING_KINDS[operation]
    return {
        "title": "The Disk May Be Failing",
        "message": f"{disk_failing_warning(evidence, kind)} {question}",
        "action_title": action_title,
        "presentation_id": disk_failing_presentation_id(operation, evidence),
    }


def disk_failing_context(evidence: DiskFailingEvidence | None) -> dict[str, str]:
    """What the confirmation id covers about the warning: its cause, so the
    user's yes to one cause's warning does not confirm the other's. Not the
    wording: a disk MaSt named on one request and not the next would ask
    again for nothing."""
    return {_DISK_FAILING_CONTEXT_KEY: evidence.cause} if evidence else {}


_DISK_FAILING_CONTEXT_KEY = "disk_failing"


def disk_failing_telemetry_fields(operation: str, evidence: DiskFailingEvidence) -> dict[str, object]:
    """disk_errors.disk_failing_fields for an app operation's warning."""
    return disk_failing_fields(evidence, _DISK_FAILING_KINDS[operation][0])


def supplied_confirmation_id(params: Mapping[str, object]) -> str:
    direct = params.get("confirmation_id")
    if isinstance(direct, str):
        return direct.strip()
    nested = params.get("confirmation")
    if isinstance(nested, Mapping):
        nested_id = nested.get("id") or nested.get("confirmation_id")
        if isinstance(nested_id, str):
            return nested_id.strip()
    return ""


def require_confirmation(
    params: Mapping[str, object],
    confirmation: ConfirmationRequest,
) -> None:
    if supplied_confirmation_id(params) == confirmation.confirmation_id:
        return
    raise AppConfirmationRequired(confirmation)


def require_disk_failing_confirmation(
    params: Mapping[str, object],
    confirmation: ConfirmationRequest,
) -> None:
    """require_confirmation for an operation the failing-disk warning applies
    to (disk_failing_context in its context). When this request found no
    evidence, a yes to the same operation and parameters with the warning,
    of either cause, confirms it too: the user agreed to a stronger warning.
    The evidence can be gone on the confirmed request (a kernel log that
    could not be read, or MaSt answering only then and the volume filter
    leaving no line), and asking again would show the plain question, whose
    default is to go on, without the disk in it. A yes to the plain question
    still does not confirm a warning, nor a yes to one cause's warning the
    other's, while the request has evidence of its own."""
    supplied = supplied_confirmation_id(params)
    if supplied and _DISK_FAILING_CONTEXT_KEY not in confirmation.context:
        warned = {
            _confirmation_id(confirmation.operation, params, {**confirmation.context, _DISK_FAILING_CONTEXT_KEY: cause})
            for cause in get_args(DiskFailingCause)
        }
        if supplied in warned:
            return
    require_confirmation(params, confirmation)
