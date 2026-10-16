from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolConfig:
    activity_window_days: int = 90
    expertise_window_days: int = 180
    expertise_half_life_days: int = 90
    recent_half_life_days: int = 14
    g3_lambda: float = 1.0
    bus_factor_share_threshold: float = 0.5
    bus_factor_min_prs: int = 10
    uncovered_person_activity_prs: int = 3
    ownership_drift_ownerless_weight: float = 0.4
    ownership_drift_stale_weight: float = 0.3
    ownership_drift_outside_top5_weight: float = 0.3
    default_top_n: int = 15
    default_max_depth: int = 2
    risk_bus_factor_weight: float = 0.35
    risk_ownership_drift_weight: float = 0.3
    risk_criticality_weight: float = 0.2
    risk_blast_radius_weight: float = 0.15


DEFAULT_TOOL_CONFIG = ToolConfig()