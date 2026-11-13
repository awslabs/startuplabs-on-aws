"""Authenticated caller, produced by the identity module, consumed by all."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    username: str
    groups: frozenset[str] = field(default_factory=frozenset)

    def in_group(self, group: str) -> bool:
        return group in self.groups
