"""AssetResolver: đọc asset theo lớp, lớp trên thắng theo tên file.

Lớp thấp → cao: packaged (_assets/) → <home>/config → --config-dir.
Neo ở files(pkg)/"_assets" rồi joinpath — KHÔNG import thư mục con (có '-', không có __init__).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from digital_development_team.core.errors import ConfigError

PKG = "digital_development_team"
ENCODING = "utf-8"


@dataclass(frozen=True, slots=True)
class Layer:
    name: str
    root: Traversable | Path

    def get(self, rel: str) -> Traversable | Path | None:
        node = self.root
        for part in rel.split("/"):
            node = node / part
        return node if node.is_file() else None

    def walk(self, subdir: str) -> Iterator[tuple[str, Traversable | Path]]:
        """Duyệt đệ quy; Traversable không có rglob."""
        base = self.root
        for part in subdir.split("/"):
            base = base / part
        if not base.is_dir():
            return
        yield from self._walk(base, subdir)

    def _walk(self, node: Traversable | Path, rel: str) -> Iterator[tuple[str, Traversable | Path]]:
        for child in node.iterdir():
            child_rel = f"{rel}/{child.name}"
            if child.is_dir():
                yield from self._walk(child, child_rel)
            else:
                yield child_rel, child


@dataclass(frozen=True, slots=True)
class AssetHit:
    rel: str
    layer: str
    node: Traversable | Path

    def read_text(self) -> str:
        return self.node.read_text(encoding=ENCODING)


class AssetResolver:
    def __init__(self, layers: Sequence[Layer]) -> None:
        if not layers:
            raise ConfigError("cần ít nhất một layer")
        self._layers = tuple(layers)  # thấp → cao

    @property
    def layer_names(self) -> tuple[str, ...]:
        return tuple(layer.name for layer in self._layers)

    def find(self, rel: str) -> AssetHit:
        for layer in reversed(self._layers):
            node = layer.get(rel)
            if node is not None:
                return AssetHit(rel=rel, layer=layer.name, node=node)
        raise ConfigError(f"không tìm thấy asset {rel!r}", layers=self.layer_names)

    def read_text(self, rel: str) -> str:
        return self.find(rel).read_text()

    def exists(self, rel: str) -> bool:
        return any(layer.get(rel) is not None for layer in self._layers)

    def list(self, subdir: str, suffix: str) -> list[AssetHit]:
        """Gộp theo đường dẫn tương đối; lớp cao thắng."""
        merged: dict[str, AssetHit] = {}
        for layer in self._layers:
            for rel, node in layer.walk(subdir):
                if rel.endswith(suffix):
                    merged[rel] = AssetHit(rel=rel, layer=layer.name, node=node)
        return sorted(merged.values(), key=lambda h: h.rel)

    def sources(self, rels: Sequence[str]) -> dict[str, str]:
        return {rel: self.find(rel).layer for rel in rels if self.exists(rel)}


def packaged_layer() -> Layer:
    return Layer(name="packaged", root=files(PKG) / "_assets")


HOME_OVERRIDES_DIR = "overrides"
"""<home>/overrides/ mirror cấu trúc _assets: config/, agents/, skills/, schemas/, checklists/.
Onboarding ghi config/sensitive_paths.yaml, config/policy.yaml (exclusive_files) vào đây."""


def build_resolver(*, home: Path | None = None, config_dir: Path | None = None) -> AssetResolver:
    """Lớp thấp → cao: packaged → <home>/overrides → --config-dir. Mọi lớp mirror cấu trúc _assets."""
    layers: list[Layer] = [packaged_layer()]
    if home is not None:
        ho = home / HOME_OVERRIDES_DIR
        if ho.is_dir():
            layers.append(Layer(name="home", root=ho))
    if config_dir is not None:
        if not config_dir.is_dir():
            raise ConfigError(f"--config-dir không tồn tại: {config_dir}")
        layers.append(Layer(name="config_dir", root=config_dir))
    return AssetResolver(layers)


ASSET_SUBDIRS = ("agents", "skills", "checklists", "tools", "prompts", "schemas", "memory")
"""Thư mục asset ngoài config/ mà worker theo dõi để dựng lại registry giữa hai issue."""


def overrides_fingerprint(home: Path | None, *, config_dir: Path | None = None) -> str:
    """Dấu vân tay rẻ (đường dẫn, mtime_ns, size) của lớp override ngoài config/. Đổi → worker dựng lại runtime.

    Không đọc nội dung file: worker gọi mỗi tick. config/ đã có ConfigProvider.fingerprint lo.
    """
    import hashlib

    h = hashlib.sha256()
    roots = [
        r for r in ((home / HOME_OVERRIDES_DIR) if home is not None else None, config_dir) if r is not None
    ]
    for root in roots:
        h.update(str(root).encode())
        for sub in ASSET_SUBDIRS:
            base = root / sub
            if not base.is_dir():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    st = p.stat()
                    h.update(f"{p.relative_to(root).as_posix()}|{st.st_mtime_ns}|{st.st_size}\n".encode())
    return h.hexdigest()
