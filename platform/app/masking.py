"""安全的单行脱敏预览生成。"""

from __future__ import annotations


PRIVATE_KEY_TYPES = {
    "private_key_block",
    "SSH 私钥",
}


def masked_preview(value: str, credential_type: str) -> str:
    """Return a bounded single-line preview that never contains the secret."""
    text = str(value or "")
    compact = text.replace("\r", " ").replace("\n", " ").strip()

    # PEM blocks and structured credentials may contain newlines in memory.
    # Never derive a preview by slicing the raw block.
    if credential_type in PRIVATE_KEY_TYPES or compact.startswith("-----BEGIN"):
        return "-----BEGIN **** KEY-----"

    if "://" in compact and ":" in compact and "@" in compact:
        prefix, rest = compact.split("://", 1)
        user, host = rest.split(":", 1)[0], rest.split("@", 1)[1]
        return f"{prefix}://{user}:****@{host}"

    if len(compact) <= 8:
        return f"{credential_type}****"

    head_length = min(6, max(1, len(compact) // 4))
    return f"{compact[:head_length]}****{compact[-4:]}"
