"""Opaque bearer secrets. Only their SHA-256 is stored."""

from __future__ import annotations

import hashlib
import secrets
from typing import Literal

TokenKind = Literal["user", "agent"]
_PREFIX: dict[TokenKind, str] = {"user": "fcl_u_", "agent": "fcl_a_"}


def issue_token(kind: TokenKind) -> tuple[str, str]:
    """Returns (token, hash). The token is shown once; the hash is stored."""
    token = _PREFIX[kind] + secrets.token_urlsafe(32)
    return token, hash_secret(token)


def hash_secret(secret: str) -> str:
    # A 256-bit random secret has nothing to brute-force, so a fast hash is enough.
    return hashlib.sha256(secret.encode()).hexdigest()


def token_kind(token: str) -> TokenKind | None:
    for kind, prefix in _PREFIX.items():
        if token.startswith(prefix):
            return kind
    return None
