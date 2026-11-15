"""Registry of role classes, keyed by role name."""
from __future__ import annotations

from typing import Callable, TypeVar

from skeleton.runtime.models import Role

T = TypeVar("T", bound=type)


class RoleRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, type] = {}

    def register(self, role_cls: T, name: str | None = None) -> T:
        """Register a role class under `name` (default: `role_cls.name`).

        Returns the class, so it also works as a decorator via `registry.register`.
        """
        key = name if name is not None else getattr(role_cls, "name", "")
        if not isinstance(key, str) or not key:
            raise ValueError("role needs a non-empty name")
        if key in self._classes:
            raise ValueError(f"role already registered: {key!r}")
        self._classes[key] = role_cls
        return role_cls

    def create(self, name: str) -> Role:
        try:
            role_cls = self._classes[name]
        except KeyError:
            known = ", ".join(sorted(self._classes)) or "none"
            raise KeyError(f"unknown role {name!r}; registered roles: {known}") from None
        return role_cls()

    def names(self) -> list[str]:
        return sorted(self._classes)

    def __contains__(self, name: object) -> bool:
        return name in self._classes
