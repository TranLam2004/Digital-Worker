"""schema → khối OUTPUT CONTRACT rút gọn cho brief (tiếng Anh — agent đọc): required + enum + ràng buộc + description
1 dòng; field lồng (object / phần tử array<object>) liệt kê thêm 1 cấp các field required hoặc có ràng buộc. Không đổ cả schema."""

from __future__ import annotations

import re
from typing import Any

_MAX_DESC = 90


def _type_of(prop: dict[str, Any]) -> str:
    t = prop.get("type")
    if isinstance(t, list):
        return "|".join(str(x) for x in t)
    if t == "array":
        items = prop.get("items", {})
        inner = items.get("type", "object") if isinstance(items, dict) else "any"
        return f"array<{inner}>"
    if t:
        return str(t)
    if "$ref" in prop:
        return "object"
    if "anyOf" in prop:
        return "|".join(_type_of(a) for a in prop["anyOf"] if isinstance(a, dict))
    return "any"


# Field do harness điền/ghi đè (HarnessFields, gate) → không đưa vào contract: agent viết cũng vô ích, và `canary`
# không được để Verifier biết tồn tại. Chỉ liệt kê field agent thật sự phải viết.
HARNESS_OWNED: dict[str, frozenset[str]] = {
    "ddt/evidence": frozenset({"version", "machine_gate", "checklist_summary", "scope_violation", "canary"}),
    "ddt/escalation": frozenset({"timeout_sec", "default_on_timeout", "resolved"}),
    "ddt/clarify": frozenset({"timeout_sec"}),
    "ddt/spec": frozenset({"amendments"}),
    # patch/: harness lấy diff + file + commit từ git (tools/blackboard.py) — model chép diff vào tool args chỉ tốn token
    "ddt/patch (PatchBundle)": frozenset({"diff", "files_touched", "base_commit", "head_commit"}),
}


def _one_line(s: str | None) -> str:
    """Mệnh đề đầu của description — điều agent cần biết, không phải cả đoạn giải thích."""
    if not s:
        return ""
    text = " ".join(s.split())
    first = re.split(r"(?<!e\.g\.)(?<!i\.e\.)(?<=[.!?])\s|\s—\s|;\s", text, maxsplit=1)[0].rstrip(".")
    return first[:_MAX_DESC]


def _leaf_condition(props: dict[str, Any], prefix: str = "") -> str:
    """{"regression": {"properties": {"ran": {"const": false}}}} → "regression.ran = false"."""
    for k, v in props.items():
        path = f"{prefix}{k}"
        if not isinstance(v, dict):
            return path
        if "const" in v:
            return f"{path} = {_json_scalar(v['const'])}"
        if "enum" in v:
            return f"{path} ∈ {'/'.join(_json_scalar(e) for e in v['enum'])}"
        if "properties" in v:
            return _leaf_condition(v["properties"], f"{path}.")
        return path
    return "?"


def _required_of(then: dict[str, Any], prefix: str = "") -> list[str]:
    out = [f"{prefix}{r}" for r in then.get("required", [])]
    for k, v in then.get("properties", {}).items():
        if isinstance(v, dict):
            out.extend(_required_of(v, f"{prefix}{k}."))
    return out


def _json_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return "null" if v is None else str(v)


def _render_condition(c: dict[str, Any]) -> str:
    cond = _leaf_condition(c["if"].get("properties", {}))
    req = _required_of(c["then"])
    return f"if {cond} → required: {', '.join(req) or '?'}"


def _constraints(p: dict[str, Any]) -> str:
    extra = ""
    if "enum" in p:
        extra = " ∈ " + " | ".join(str(e) for e in p["enum"])
    if "minimum" in p or "maximum" in p:
        extra += f" [{p.get('minimum', '')}..{p.get('maximum', '')}]"
    if "pattern" in p:
        extra += f" /{p['pattern']}/"
    if "maxLength" in p:
        extra += f" ≤{p['maxLength']} chars"
    if "maxItems" in p:
        extra += f" ≤{p['maxItems']} items"
    return extra


def _nested_lines(node: dict[str, Any], root: dict[str, Any]) -> list[str]:
    """Field con của object / phần tử array<object>: chỉ field required hoặc có ràng buộc (enum, maxLength, pattern…)."""
    if "$ref" in node and str(node["$ref"]).startswith("#/"):
        cur: Any = root
        for part in str(node["$ref"])[2:].split("/"):
            cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        node = cur if isinstance(cur, dict) else node
    if node.get("type") == "array" and isinstance(node.get("items"), dict):
        node = node["items"]
    props = node.get("properties") or {}
    req = set(node.get("required") or [])
    out = []
    for k, sp in props.items():
        if not isinstance(sp, dict):
            continue
        cons = _constraints(sp)
        items = sp.get("items") if sp.get("type") == "array" else None
        if isinstance(items, dict) and items.get("properties"):  # hình dạng phần tử: {*path, symbols, lines}
            ireq = set(items.get("required") or [])
            cons += (
                " items {" + ", ".join(f"{'*' if ik in ireq else ''}{ik}" for ik in items["properties"]) + "}"
            )
        if k in req or cons:
            out.append(f"    · {'*' if k in req else ''}{k}: {_type_of(sp)}{cons}")
    return out[:10]


def render_contract(schema: dict[str, Any], slot: str, *, include_optional: bool = True) -> str:
    """Khối text cho brief. Required đánh dấu *. Enum/ràng buộc liệt kê, kể cả field lồng 1 cấp."""
    hidden = HARNESS_OWNED.get(str(schema.get("$id", "")), frozenset()) | (
        {"issue_id"} if "issue_id" in schema.get("properties", {}) else frozenset()
    )  # FileStore.write luôn đặt issue_id = issue thật
    props: dict[str, Any] = {k: v for k, v in schema.get("properties", {}).items() if k not in hidden}
    required = set(schema.get("required", [])) - hidden
    lines = [f"Write `{slot}` following schema `{schema.get('$id', '?')}` (* = required):"]
    if hidden:
        lines.append(f"(harness fills: {', '.join(sorted(hidden - {'canary'}))} — do not write them)")
    ordered = [k for k in props if k in required] + (
        [k for k in props if k not in required] if include_optional else []
    )
    for key in ordered:
        p = props[key]
        mark = "*" if key in required else " "
        typ = _type_of(p)
        extra = ""
        if "enum" in p:
            extra = " ∈ " + " | ".join(str(e) for e in p["enum"])
        elif typ.startswith("array") and isinstance(p.get("items"), dict) and "enum" in p["items"]:
            extra = " items ∈ " + " | ".join(str(e) for e in p["items"]["enum"])
        for k in ("minimum", "maximum", "pattern", "maxLength", "maxItems"):
            if k in p:
                extra += _constraints({k: p[k]})
        desc = _one_line(p.get("description"))
        lines.append(f"- {mark}{key}: {typ}{extra}" + (f" — {desc}" if desc else ""))
        if typ.startswith("array<object") or typ == "object" or "$ref" in p:
            lines.extend(_nested_lines(p, schema))
    conds = schema.get("allOf", [])
    rendered = [_render_condition(c) for c in conds if "if" in c and "then" in c]
    if rendered:
        lines.append("Conditions:")
        lines.extend(f"- {r}" for r in rendered)
    lines.append(
        'If you cannot produce it: return {"status": "blocked", "reason": "<specific reason>"} and STOP.'
    )
    return "\n".join(lines)
