"""Small, local-only helpers for candidate filtering."""

from __future__ import annotations

import math
import re


PLACEHOLDER_TOKENS = (
    "example",
    "your_",
    "your-",
    "xxxx",
    "changeme",
    "placeholder",
    "dummy",
    "sample",
    "todo",
    "fixme",
    "fake",
    "<",
    ">",
    "{",
    "}",
    "insert_",
    "apikey_here",
    "token_here",
    "secret_here",
    "********",
)


def is_placeholder(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if any(token in lowered for token in PLACEHOLDER_TOKENS):
        return True
    normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if normalized in {
        "api",
        "api_key",
        "api_keys",
        "api_token",
        "access_token",
        "auth_token",
        "secret_key",
        "private_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "openai_api_key",
        "stackoverflow_config_secret",
        "config_assignment_secret",
    }:
        return True
    if re.fullmatch(
        r"(?:my|your)_(?:api_)?(?:key|token|secret)(?:_.+)?",
        normalized,
    ):
        return True
    if len(text) >= 12 and len(set(text)) <= 4:
        return True
    return lowered in {
        "password",
        "passwd",
        "pass",
        "pwd",
        "secret",
        "token",
        "key",
        "none",
        "null",
        "true",
        "false",
    }


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )
