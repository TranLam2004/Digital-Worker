"""Checklists — tiêu chí đúng theo artifact × project-type × domain (35-checklists). Dữ liệu YAML, 0 LLM cho regex.

    reg = ChecklistRegistry(resolver)
    items = reg.select("patch", scope, project_type=None, domain=None)       # applies_when qua ddt.expr
    run = run_regex(items, diff)                                              # pha 1 — máy
    run.blocking_fail · run.clean · run.security_regex_pass · run.results      # facts cho transitions / evidence

verify: regex (P1) · test (P2: coverage/sast) · diff_inspection/llm (Verifier đọc, P2 nạp vào brief) · learned (P3).
"""

from __future__ import annotations

from digital_development_team.checklist.loader import ChecklistItem, ChecklistRegistry
from digital_development_team.checklist.runner import ChecklistRun, ItemResult, run_regex

__all__ = ["ChecklistItem", "ChecklistRegistry", "ChecklistRun", "ItemResult", "run_regex"]
