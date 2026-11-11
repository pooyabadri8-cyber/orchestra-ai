"""Content-addressed file store for images: a file is named by its SHA-256, so a stored file can be checked against the
hash in the package right before it is sent."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class AssetIntegrityError(Exception):
    """A stored file no longer matches the hash it was approved with."""


class AssetStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)  # atomic on Windows and POSIX
        return sha

    def get(self, sha256: str) -> bytes:
        """The bytes, verified against their name. Raises AssetIntegrityError on a missing or changed file."""
        path = self._path(sha256)
        try:
            data = path.read_bytes()
        except OSError as e:
            raise AssetIntegrityError(f"asset {sha256[:12]} missing: {e}") from e
        if hashlib.sha256(data).hexdigest() != sha256:
            raise AssetIntegrityError(f"asset {sha256[:12]} changed on disk")
        return data
