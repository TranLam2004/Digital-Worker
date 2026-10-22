"""YAML loader báo lỗi khi trùng key (PyYAML mặc định im lặng, giá trị sau thắng)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from digital_development_team.core.errors import ConfigError
from digital_development_team.util.fsio import read_text, write_text_atomic

_BaseLoader: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)  # libyaml: nhanh ~10×


class _StrictLoader(_BaseLoader):  # type: ignore[misc,valid-type]
    """Mỗi process `ddt mcp` (một lần mỗi agent run) parse ~46 file YAML — parser C cắt ~0.4 s mỗi lần spawn."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ConfigError(
                f"trùng key {key!r} trong YAML",
                hint="PyYAML mặc định lấy giá trị sau; DDT từ chối để tránh config ngầm sai",
                line=key_node.start_mark.line + 1,
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def loads(text: str, *, source: str = "<string>") -> Any:
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 — SafeLoader subclass
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML không hợp lệ trong {source}: {e}") from e


def load_file(path: Path) -> Any:
    return loads(read_text(path), source=str(path))


def dumps(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=110)


def save_file(path: Path, data: Any, *, fsync: bool = True) -> None:
    write_text_atomic(path, dumps(data), fsync=fsync)
