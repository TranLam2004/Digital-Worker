"""Reporting — báo cáo 1 trang từ events.jsonl + board + checkpoint (06-event-log). 0 LLM."""

from __future__ import annotations

from digital_development_team.reporting.handover import Handover, build_handover
from digital_development_team.reporting.report import SessionReport, build_report, render_report
from digital_development_team.reporting.stats import RoleStat, derive_role_stats, role_stats_json

__all__ = [
    "Handover",
    "RoleStat",
    "SessionReport",
    "build_handover",
    "build_report",
    "derive_role_stats",
    "render_report",
    "role_stats_json",
]
