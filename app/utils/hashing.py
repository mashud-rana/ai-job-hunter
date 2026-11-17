from __future__ import annotations

import hashlib

from app.utils.text import normalize_key


def content_hash(title: str, company: str | None, location: str | None) -> str:
    """Stable fingerprint of a posting; catches the same job cross-posted to several sources."""
    basis = "|".join(normalize_key(x) for x in (title, company or "", location or ""))
    return hashlib.sha256(basis.encode()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
