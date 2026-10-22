"""Nạp config theo lớp (packaged → <home>/config → --config-dir → dotted patches), Pydantic, snapshot, check.

Policy là dữ liệu, không phải logic. Mọi hằng số vận hành ở _assets/config/*.yaml.
"""

from __future__ import annotations

from digital_development_team.config.loader import ResolvedConfig, load_config
from digital_development_team.config.provider import ConfigProvider
from digital_development_team.config.resources import AssetResolver, build_resolver

__all__ = ["AssetResolver", "ConfigProvider", "ResolvedConfig", "build_resolver", "load_config"]
