from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

DayMode = Literal["normal", "cautious"]


@dataclass
class DayAssessment:
    mode: DayMode
    contract_multiplier: float
    reasons: list[str] = field(default_factory=list)
    assessed_at: datetime = field(default_factory=datetime.now)
