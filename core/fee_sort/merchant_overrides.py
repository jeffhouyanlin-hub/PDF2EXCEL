"""Persistent merchant → category overrides learned from user edits.

A transaction's Category can be overridden per merchant signature. During
classification, the override is checked BEFORE any regex rule (highest
priority). Users add overrides via the fee_sort_page "save as rule" button;
entries persist across sessions in a JSON file.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

_DEFAULT_PATH = Path(__file__).parent / "merchant_overrides.json"


def merchant_signature(description: str) -> str:
    """Compact signature grouping near-duplicate merchant strings.

    Strips digit runs (store/order IDs) and keeps the first 3 meaningful
    tokens, uppercased. Intentionally imperfect — it gives a reasonable
    match for typical RBC Mastercard descriptions (e.g. "IMPARK00011650U
    DELTA BC" → "IMPARK DELTA BC"), but the user can always add a fresh
    override when a new variant appears.
    """
    cleaned = re.sub(r"\d+[A-Z]*", "", description)
    cleaned = re.sub(r"[#*]", " ", cleaned)
    cleaned = re.sub(r"[^\w\s'&\-.]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().upper()
    return " ".join(cleaned.split()[:3])


@dataclass
class Override:
    """One learned merchant→category mapping."""

    category: str
    last_desc: str
    count: int = 1
    updated: str = ""


class MerchantOverrides:
    """File-backed store mapping merchant signature → CCCategory value."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._data: dict[str, Override] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = {k: Override(**v) for k, v in raw.items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            # Corrupt file — start fresh but don't crash classification.
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        raw = {k: asdict(v) for k, v in self._data.items()}
        self._path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Lookup / mutation
    # ------------------------------------------------------------------

    def lookup(self, description: str) -> str | None:
        """Return the override category for a description, or None."""
        sig = merchant_signature(description)
        entry = self._data.get(sig)
        return entry.category if entry else None

    def add(self, description: str, category: str) -> bool:
        """Add or update an override. Returns True if this is a new entry."""
        sig = merchant_signature(description)
        existing = self._data.get(sig)
        is_new = existing is None or existing.category != category
        self._data[sig] = Override(
            category=category,
            last_desc=description,
            count=(existing.count + 1) if (existing and existing.category == category) else 1,
            updated=datetime.now().isoformat(timespec="seconds"),
        )
        self._save()
        return is_new

    def remove(self, signature: str) -> bool:
        """Remove an override by signature. Returns True if it existed."""
        if signature in self._data:
            del self._data[signature]
            self._save()
            return True
        return False

    def all(self) -> list[tuple[str, Override]]:
        """Return all overrides sorted by signature."""
        return sorted(self._data.items())

    def clear(self) -> None:
        """Remove all overrides."""
        self._data = {}
        self._save()

    def __len__(self) -> int:
        return len(self._data)
