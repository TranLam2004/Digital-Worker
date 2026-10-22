"""Runtime patch — lớp 3b của config: <home>/overrides/config/runtime.yaml, chỉ chứa patch dotted, không thay nguyên file.

    preset: economy                 # tuỳ chọn, áp như --preset khi CLI không truyền
    flags: [--no-canary]            # tuỳ chọn, nối vào flags CLI
    policy:  { autonomy: aggressive, wip_limit: 2 }
    budgets: { stages.IMPLEMENT.T1.turns: 30 }   # dotted hoặc lồng nhau đều được

Web (Settings) và `ddt runtime set` ghi vào đây; ConfigProvider thấy mtime đổi → worker emit config_reloaded ở vòng kế.
Model registry dựng một lần khi khởi động: đổi `models.*` cần restart worker (loader vẫn áp để `config check` thấy).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from digital_development_team.config.resources import HOME_OVERRIDES_DIR
from digital_development_team.core.errors import ConfigError
from digital_development_team.util import yamlio
from digital_development_team.util.fsio import ENCODING, write_text_atomic

RUNTIME_REL = "config/runtime.yaml"
SECTIONS = ("policy", "budgets", "models", "sensitive_paths")
META_KEYS = ("preset", "flags")


def runtime_path(home: Path) -> Path:
    return home / HOME_OVERRIDES_DIR / RUNTIME_REL


def read_runtime(home: Path | None) -> dict[str, Any]:
    if home is None:
        return {}
    p = runtime_path(home)
    if not p.is_file():
        return {}
    data = yamlio.loads(p.read_text(encoding=ENCODING), source=str(p))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: gốc phải là mapping")
    for k in data:
        if k not in SECTIONS and k not in META_KEYS:
            raise ConfigError(f"{p}: key lạ {k!r}", hint=f"cho phép: {', '.join(SECTIONS + META_KEYS)}")
    return dict(data)


def flatten(d: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """{'a': {'b': 1}, 'c.d': 2} → ('a.b', 1), ('c.d', 2). Dict lá rỗng cũng là giá trị."""
    if isinstance(d, dict) and d:
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            yield from flatten(v, key)
    elif prefix:
        yield prefix, d


def write_runtime(home: Path, data: dict[str, Any]) -> Path:
    p = runtime_path(home)
    clean = {k: v for k, v in data.items() if v not in (None, {}, [])}
    if not clean:
        if p.is_file():
            p.unlink()
        return p
    header = (
        "# runtime.yaml — patch dotted do web/`ddt runtime set` ghi. Hiệu lực ở issue kế tiếp (hot-reload).\n"
    )
    write_text_atomic(p, header + yamlio.dumps(clean), fsync=False)
    return p


def set_runtime(home: Path, section: str, dotted: str | None, value: Any) -> dict[str, Any]:
    """set_runtime(home, 'policy', 'autonomy', 'aggressive') · set_runtime(home, 'preset', None, 'economy')."""
    data = read_runtime(home)
    if section in META_KEYS:
        data[section] = value
    elif section in SECTIONS:
        if not dotted:
            raise ConfigError("cần dotted key, vd policy.autonomy")
        sec = data.get(section)
        sec = dict(sec) if isinstance(sec, dict) else {}
        sec[dotted] = value
        data[section] = sec
    else:
        raise ConfigError(f"section lạ {section!r}", hint=f"{', '.join(SECTIONS + META_KEYS)}")
    write_runtime(home, data)
    return data


def unset_runtime(home: Path, section: str, dotted: str | None = None) -> dict[str, Any]:
    data = read_runtime(home)
    if section in META_KEYS or dotted is None:
        data.pop(section, None)
    else:
        sec = data.get(section)
        if isinstance(sec, dict):
            sec = dict(sec)
            sec.pop(dotted, None)
            data[section] = sec
    write_runtime(home, data)
    return data


def clear_runtime(home: Path) -> bool:
    p = runtime_path(home)
    if p.is_file():
        p.unlink()
        return True
    return False
