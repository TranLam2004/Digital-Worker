"""Chuẩn hóa artifact theo schema TRƯỚC khi validate — chỉ sửa giá trị chắc chắn bị từ chối và sửa được mà không đổi nghĩa.

Quy tắc (mỗi quy tắc chỉ chạy khi giá trị hiện tại vi phạm subschema):
  enum      "pass (13 passed)" / "PASS" / "Pass." → "pass"   (giá trị bắt đầu bằng đúng 1 phần tử enum + ký tự không phải chữ/số)
  maxLength chuỗi dài hơn → cắt còn maxLength, ký tự cuối "…"
  object    chuỗi "shop/cart.py" ở chỗ cần object có đúng 1 field required kiểu string → {"path": "shop/cart.py"}
  pattern   field TÙY CHỌN kiểu string sai pattern (vd lines: "end of file") → bỏ field đó (máy không dùng được giá trị)
Không đoán kiểu khác, không thêm field; chỉ bỏ field tùy chọn không hợp lệ. Trả (data mới, danh sách ghi chú "path: mô tả") để emit cho audit.

Lý do: chạy thật qua Claude CLI, agent hay ghi `summary_for_human.regression: "pass (13 passed)"` hoặc `reason` dài 230 ký tự;
mỗi lần như vậy tốn 1 lượt sửa schema (token + thời gian) cho một lỗi định dạng thuần.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_ELLIPSIS = "…"


def _resolve(node: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        cur: Any = root
        for part in ref[2:].split("/"):
            cur = cur.get(part, {}) if isinstance(cur, Mapping) else {}
        return cur if isinstance(cur, Mapping) else node
    return node


def _types(node: Mapping[str, Any]) -> set[str]:
    t = node.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {str(x) for x in t}
    return set()


def _coerce_enum(value: str, enum: list[Any]) -> str | None:
    if value in enum:
        return None
    low = value.strip().lower()
    str_enum = [e for e in enum if isinstance(e, str)]
    exact = [e for e in str_enum if e.lower() == low]
    if len(exact) == 1:
        return exact[0]
    hits = [e for e in str_enum if re.match(rf"{re.escape(e.lower())}(?![a-z0-9_])", low)]
    # chọn phần tử dài nhất khớp đầu chuỗi (vd "needs_human" thắng "needs") — phải duy nhất theo độ dài
    if hits:
        best = max(hits, key=len)
        if sum(1 for h in hits if len(h) == len(best)) == 1:
            return best
    return None


def _walk(value: Any, node: Mapping[str, Any], root: Mapping[str, Any], path: str, notes: list[str]) -> Any:
    node = _resolve(node, root)
    types = _types(node)
    if isinstance(value, str):
        if isinstance(node.get("enum"), list):
            fixed = _coerce_enum(value, node["enum"])
            if fixed is not None:
                notes.append(f"{path}: {value[:40]!r} → {fixed!r} (enum)")
                return fixed
        max_len = node.get("maxLength")
        if isinstance(max_len, int) and len(value) > max_len >= 2:
            notes.append(f"{path}: cut {len(value)} → {max_len} chars (maxLength)")
            return value[: max_len - 1].rstrip() + _ELLIPSIS
        if types == {"object"}:
            req = node.get("required") or []
            props = node.get("properties") or {}
            if len(req) == 1 and _types(_resolve(props.get(req[0], {}), root)) == {"string"}:
                notes.append(f"{path}: string → {{{req[0]!r}: …}} (object with one required field)")
                return {req[0]: value}
        return value
    if isinstance(value, Mapping):
        props = node.get("properties") or {}
        if not props:
            return value
        out = dict(value)
        req = set(node.get("required") or [])
        for k, sub in props.items():
            if k in out and isinstance(sub, Mapping):
                kp = f"{path}.{k}" if path else k
                out[k] = _walk(out[k], sub, root, kp, notes)
                rs = _resolve(sub, root)
                pat = rs.get("pattern")
                if (
                    k not in req
                    and isinstance(out[k], str)
                    and isinstance(pat, str)
                    and not re.search(pat, out[k])
                ):
                    notes.append(f"{kp}: dropped {out[k][:40]!r} (optional, does not match /{pat}/)")
                    del out[k]
        return out
    if isinstance(value, list):
        items = node.get("items")
        if not isinstance(items, Mapping):
            return value
        return [_walk(v, items, root, f"{path}[{i}]", notes) for i, v in enumerate(value)]
    return value


def coerce_to_schema(schema: Mapping[str, Any], data: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    out = _walk(data, schema, schema, "", notes)
    return (dict(out) if isinstance(out, Mapping) else dict(data)), notes
