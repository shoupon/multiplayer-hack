from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeniedAudit:
    """Attached to authorization denials so they get audited outside the rolled-back transaction."""

    action: str
    family_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, denied: DeniedAudit | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.denied = denied


def bad_request(code: str, message: str) -> AppError:
    return AppError(400, code, message)


def unauthenticated() -> AppError:
    return AppError(401, "unauthenticated", "missing, invalid, or revoked credentials")


def forbidden(code: str, message: str, denied: DeniedAudit | None = None) -> AppError:
    return AppError(403, code, message, denied)


def not_found(what: str) -> AppError:
    return AppError(404, "not_found", f"{what} not found")


def conflict(code: str, message: str) -> AppError:
    return AppError(409, code, message)


def unprocessable(code: str, message: str) -> AppError:
    return AppError(422, code, message)
