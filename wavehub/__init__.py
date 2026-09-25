"""门店配送波次中枢。

完整的波次编排与交接系统：需求合并、截单、波次编排、配送版本、
扫描交接（离线补传、幂等）、并发封签、整车转派与门店查询。
"""
from __future__ import annotations

from .errors import (
    CutoffError,
    InfeasiblePlanError,
    NotFoundError,
    SealConflictError,
    StateError,
    TransferError,
    VersionMismatchError,
    WaveHubError,
)
from .service import WaveHub

__all__ = [
    "WaveHub",
    "WaveHubError",
    "NotFoundError",
    "StateError",
    "CutoffError",
    "VersionMismatchError",
    "SealConflictError",
    "InfeasiblePlanError",
    "TransferError",
]
