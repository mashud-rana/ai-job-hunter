"""Where the JSON files live: a local folder, or a private GitHub Gist (survives ephemeral hosts)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from app.logging import get_logger

log = get_logger(__name__)


class StorageError(RuntimeError):
    pass


class StorageBackend(ABC):
    name: str

    @abstractmethod
    async def read_all(self) -> dict[str, str]:
        """Return {filename: text} for every stored file."""

    @abstractmethod
    async def write(self, files: dict[str, str]) -> None:
        """Persist the given files (others untouched)."""

    async def aclose(self) -> None:
        return None


class LocalBackend(StorageBackend):
    """JSON files in a directory. Writes are atomic (temp file + os.replace)."""

    name = "local"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def read_all(self) -> dict[str, str]:
        return await asyncio.to_thread(self._read_all)

    def _read_all(self) -> dict[str, str]:
        if not self.directory.exists():
            return {}
        return {p.name: p.read_text(encoding="utf-8") for p in self.directory.glob("*.json")}

    async def write(self, files: dict[str, str]) -> None:
        await asyncio.to_thread(self._write, files)

    def _write(self, files: dict[str, str]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.directory / name)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise


class GistBackend(StorageBackend):
    """Files in one private GitHub Gist (token needs only the `gist` scope).

    Useful on hosts whose disk is wiped on restart (e.g. Render free instances).
    """

    name = "gist"
    API = "https://api.github.com/gists/"

    def __init__(self, gist_id: str, token: str, client: httpx.AsyncClient | None = None) -> None:
        if not gist_id or not token:
            raise ValueError("GIST_ID and GITHUB_TOKEN are required for STORAGE_BACKEND=gist")
        self.url = self.API + gist_id
        self._owns = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AIJobHunter/0.1",
        }

    async def read_all(self) -> dict[str, str]:
        try:
            resp = await self.client.get(self.url, headers=self.headers)
            resp.raise_for_status()
            files = resp.json().get("files") or {}
            out: dict[str, str] = {}
            for name, meta in files.items():
                if not name.endswith(".json"):
                    continue
                if meta.get("truncated"):  # > 1 MB: fetch the raw file (up to 10 MB)
                    raw = await self.client.get(meta["raw_url"], headers=self.headers)
                    raw.raise_for_status()
                    out[name] = raw.text
                else:
                    out[name] = meta.get("content") or ""
            return out
        except httpx.HTTPError as exc:
            raise StorageError(f"cannot read gist: {type(exc).__name__}") from None

    async def write(self, files: dict[str, str]) -> None:
        payload = {"files": {name: {"content": text} for name, text in files.items()}}
        try:
            resp = await self.client.patch(self.url, headers=self.headers, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # never include the token; status code only
            status = getattr(getattr(exc, "response", None), "status_code", "")
            raise StorageError(f"cannot write gist: {type(exc).__name__} {status}".strip()) from None

    async def aclose(self) -> None:
        if self._owns:
            await self.client.aclose()
