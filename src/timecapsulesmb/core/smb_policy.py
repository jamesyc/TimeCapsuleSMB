from __future__ import annotations


SMB_PROTOCOL_ENCRYPTION_ERROR = (
    "Require SMB Encryption cannot be used with Allow Any SMB Protocol; "
    "SMB encryption requires SMB3-only negotiation."
)
SMB_ENCRYPTION_DISABLE_CONFLICT_ERROR = (
    "Require SMB Encryption cannot be used with Turn Off SMB Encryption."
)


def validate_smb_protocol_options(
    *,
    any_protocol: bool,
    require_smb_encryption: bool,
    disable_smb_encryption: bool = False,
) -> None:
    if any_protocol and require_smb_encryption:
        raise ValueError(SMB_PROTOCOL_ENCRYPTION_ERROR)
    if require_smb_encryption and disable_smb_encryption:
        raise ValueError(SMB_ENCRYPTION_DISABLE_CONFLICT_ERROR)
