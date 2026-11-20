"""Throwaway passwords generated once per test session: no credentials in the repo.

Each one satisfies the user pool policy (upper, lower, digit, symbol, 8+ chars).
"""

from __future__ import annotations

import secrets


def make_password() -> str:
    return f"{secrets.token_urlsafe(12)}Aa1-"


PASSWORD = make_password()
WRONG_PASSWORD = make_password()
SHORT_PASSWORD = "x" * 5  # below every policy minimum
