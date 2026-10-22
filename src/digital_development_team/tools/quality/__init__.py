"""tools.quality — parser thuần cho output scanner/coverage (0 subprocess, 0 LLM). Thực thi nằm ở tool gọi `sandbox.run`.

Mọi parser trả dict JSON-serializable, finding chuẩn hóa về dạng `sec_report.findings[]`:
  {severity: critical|high|medium|low|info, cwe_or_category, location: "file:line", description, tool, rule_id}
Không bao giờ chép secret vào output — chỉ vị trí + loại.
"""

from __future__ import annotations

from digital_development_team.tools.quality.bandit import parse_bandit
from digital_development_team.tools.quality.coverage_xml import parse_coverage_xml
from digital_development_team.tools.quality.detect_secrets import parse_detect_secrets
from digital_development_team.tools.quality.diff_cover import parse_diff_cover
from digital_development_team.tools.quality.pip_audit import parse_pip_audit

__all__ = [
    "parse_bandit",
    "parse_coverage_xml",
    "parse_detect_secrets",
    "parse_diff_cover",
    "parse_pip_audit",
]
