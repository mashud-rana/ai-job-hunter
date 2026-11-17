"""Text helpers: HTML to text, whitespace cleanup, safe truncation."""

from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup

_WS = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL = re.compile(r"\n{3,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    if "<" not in html and "&" not in html:
        return clean_text(html)
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n")
    return clean_text(text)


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL.sub("", text)
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    return _MULTI_NL.sub("\n\n", "\n".join(lines)).strip()


def normalize_key(text: str | None) -> str:
    """Lowercase, alphanumeric-only form used for hashing / comparisons."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix
