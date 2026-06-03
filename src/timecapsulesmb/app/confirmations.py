from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping

from timecapsulesmb.services.app import AppOperationError, jsonable


CONFIRMATION_SCHEMA_VERSION = 1
_CONFIRMATION_ONLY_KEYS = frozenset({
    "confirmation_id",
    "confirmation",
})
_SECRET_PARAM_KEYS = frozenset({"password", "credentials"})


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
        super().__init__(confirmation.message, code="confirmation_required")
        self.confirmation = confirmation


def _safe_params(params: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): value
        for key, value in params.items()
        if str(key) not in _CONFIRMATION_ONLY_KEYS and str(key) not in _SECRET_PARAM_KEYS
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
