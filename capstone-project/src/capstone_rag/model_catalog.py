"""Authoritative fictional device-model vocabulary.

Device identity is established by ERP serial resolution.  Text aliases are
only model references: they never establish a device, revision, or ownership.
Unsupported historical product names are retained explicitly so callers can
route them to clarification instead of silently remapping them.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional


SUPPORTED_MODELS = (
    "P2400", "P3410", "P4800", "Series-M", "3-Series", "Home-NB",
)

MODEL_ALIASES = {
    "P2400": ("p2400", "p-2400", "p 2400"),
    "P3410": ("p3410", "p-3410", "p 3410"),
    "P4800": ("p4800", "p-4800", "p 4800"),
    "Series-M": ("series-m", "series m"),
    "3-Series": ("3-series", "3 series"),
    "Home-NB": ("home-nb", "home nb"),
}

# These names occur in historical synthetic tickets.  They are not aliases
# for a supported model and must not be used for QMS applicability.
UNSUPPORTED_REFERENCES = {
    "P100": ("p100", "p-100", "p 100"),
    "100XL": ("100xl", "100 xl"),
    "Aquex": ("aquex",),
}


@dataclass(frozen=True)
class ModelReference:
    raw: str
    canonical: Optional[str]
    status: str
    start: int
    end: int

    @property
    def supported(self) -> bool:
        return self.status == "supported"


def _matches(text: str, vocabulary):
    for canonical, aliases in vocabulary.items():
        for alias in aliases:
            pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
            match = re.search(pattern, text or "", re.IGNORECASE)
            if match:
                yield match.start(), canonical, match


def resolve_reference(text: str) -> Optional[ModelReference]:
    """Return the earliest controlled model reference in ``text``."""
    found = []
    found.extend((start, canonical, match, "supported")
                 for start, canonical, match in _matches(text, MODEL_ALIASES))
    found.extend((start, canonical, match, "unsupported")
                 for start, canonical, match in _matches(text, UNSUPPORTED_REFERENCES))
    if not found:
        return None
    _, canonical, match, status = min(found, key=lambda row: row[0])
    return ModelReference(match.group(0), canonical if status == "supported" else None,
                          status, match.start(), match.end())


def is_supported(model: str) -> bool:
    return model in SUPPORTED_MODELS

