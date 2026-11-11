"""Loads the knowledge base the agent is allowed to draw facts from."""
from __future__ import annotations

from pathlib import Path


def load_knowledge(directory: str | Path) -> str:
    d = Path(directory)
    if not d.is_dir():
        raise SystemExit(f"پوشهٔ پایگاه دانش پیدا نشد: {d}")
    parts: list[str] = []
    for p in sorted(d.rglob("*")):
        if p.suffix.lower() in {".md", ".txt"} and p.is_file():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                parts.append(f"<document name=\"{p.name}\">\n{text}\n</document>")
    if not parts:
        raise SystemExit(f"هیچ فایل .md یا .txt در {d} نیست؛ agent بدون منبع نمی‌تواند بنویسد.")
    return "\n\n".join(parts)
