"""Keyed credential fingerprints; the key is never persisted by this module."""

from __future__ import annotations

import hashlib
import hmac


class FingerprintConfigurationError(ValueError):
    """Raised when a protected fingerprint key is absent or unsafe."""


def credential_fingerprint(secret: str, key: str) -> str:
    if not key or len(key) < 16:
        raise FingerprintConfigurationError("credential HMAC key is missing or too short")
    if "\x00" in key or "\n" in key or "\r" in key:
        raise FingerprintConfigurationError("credential HMAC key contains invalid characters")
    return hmac.new(key.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).hexdigest()
