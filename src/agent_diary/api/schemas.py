from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchRequest:
    query: str
    limit: int = 20
    filters: dict[str, Any] = field(default_factory=dict)
