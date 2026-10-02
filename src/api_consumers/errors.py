"""Errores de dominio → RFC 7807 (application/problem+json)."""

from __future__ import annotations


class Problem(Exception):
    def __init__(self, status: int, title: str, detail: str = "", type_: str = "about:blank"):
        super().__init__(f"{status} {title}: {detail}")
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type_

    def to_dict(self, instance: str = "") -> dict:
        d = {"type": self.type, "title": self.title, "status": self.status, "detail": self.detail}
        if instance:
            d["instance"] = instance
        return d


def not_found(detail: str) -> Problem:
    return Problem(404, "Not Found", detail)


def conflict(detail: str) -> Problem:
    return Problem(409, "Conflict", detail)


def unprocessable(detail: str) -> Problem:
    return Problem(422, "Unprocessable Entity", detail)


def not_implemented(detail: str) -> Problem:
    return Problem(501, "Not Implemented", detail)
