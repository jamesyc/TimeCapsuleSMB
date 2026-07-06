from __future__ import annotations


SMB_PROTOCOL_ENCRYPTION_ERROR = (
    "Require SMB Encryption cannot be used with Allow Any SMB Protocol; "
    "SMB encryption requires SMB3-only negotiation."
)


def validate_smb_protocol_options(*, any_protocol: bool, require_smb_encryption: bool) -> None:
    if any_protocol and require_smb_encryption:
        raise ValueError(SMB_PROTOCOL_ENCRYPTION_ERROR)
