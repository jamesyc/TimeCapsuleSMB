from __future__ import annotations


SMB_PROTOCOL_ENCRYPTION_ERROR = (
    "Require SMB Encryption cannot be used with Allow Any SMB Protocol; "
    "SMB encryption requires SMB3-only negotiation."
)
SMB_ENCRYPTION_DISABLE_CONFLICT_ERROR = (
    "Require SMB Encryption cannot be used with Force Disable SMB Signing and Encryption."
)


def validate_smb_protocol_options(
    *,
    any_protocol: bool,
    require_smb_encryption: bool,
    force_disable_smb_signing_and_encryption: bool = False,
) -> None:
    if any_protocol and require_smb_encryption:
        raise ValueError(SMB_PROTOCOL_ENCRYPTION_ERROR)
    if require_smb_encryption and force_disable_smb_signing_and_encryption:
        raise ValueError(SMB_ENCRYPTION_DISABLE_CONFLICT_ERROR)
