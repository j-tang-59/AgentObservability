"""Content-addressed local filesystem store (D8).

Payloads never go into span attributes — Tempo caps bytes/trace and full
prompts/tool outputs would blow that cap. Spans instead carry a
sha256-keyed reference; the evaluator resolves the reference lazily.

No standard reference-format exists yet for this (the GenAI spec's
content-upload hook leaves it as a TODO), so agenttrace.content.ref is a
plain "sha256:<hex>" string, and the store resolves it to a path.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def canonicalize(text: str) -> str:
    return text.strip()


def sha256_of(text: str) -> str:
    return hashlib.sha256(canonicalize(text).encode("utf-8")).hexdigest()


class ContentStore:
    """Local filesystem implementation. An S3/MinIO store can satisfy the
    same three-method interface (put/get/exists) for production use."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or os.environ.get("AGENTTRACE_CONTENT_STORE", "./.agenttrace_content"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / f"{digest}.txt"

    def put(self, text: str) -> tuple[str, str]:
        """Store text; returns (ref, sha256). ref is "sha256:<hex>"."""
        digest = sha256_of(text)
        path = self._path_for(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canonicalize(text), encoding="utf-8")
        return f"sha256:{digest}", digest

    def get(self, ref: str) -> str:
        if not ref.startswith("sha256:"):
            raise ValueError(f"unsupported content ref format: {ref}")
        digest = ref.removeprefix("sha256:")
        path = self._path_for(digest)
        if not path.exists():
            raise FileNotFoundError(f"content not found for {ref}")
        return path.read_text(encoding="utf-8")

    def exists(self, ref: str) -> bool:
        try:
            digest = ref.removeprefix("sha256:")
            return self._path_for(digest).exists()
        except Exception:  # noqa: BLE001 - existence check must never raise
            return False


_default_store: ContentStore | None = None


def get_default_store() -> ContentStore:
    global _default_store
    if _default_store is None:
        _default_store = ContentStore()
    return _default_store
