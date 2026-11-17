from __future__ import annotations

import copy
from typing import Any

from app.database.store import JsonStore


class SettingsRepository:
    """Key/value preferences, persisted in state.json."""

    def __init__(self, store: JsonStore) -> None:
        self.store = store

    def _bag(self) -> dict[str, Any]:
        return self.store.state.setdefault("settings", {})

    async def get(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self._bag().get(key, default))

    async def set(self, key: str, value: Any) -> None:
        self._bag()[key] = copy.deepcopy(value)
        self.store.touch("state")

    async def all(self) -> dict[str, Any]:
        return copy.deepcopy(self._bag())
