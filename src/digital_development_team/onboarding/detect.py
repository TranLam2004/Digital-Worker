"""Nhận diện ngôn ngữ / công cụ của repo từ file đánh dấu (0 LLM, không chạy gì) → lệnh ứng viên để probe.

Một repo có thể nhiều ngôn ngữ (vd backend Java + frontend JS): mỗi stack một bộ lệnh, probe giữ lệnh chạy được.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath


@dataclass
class Stack:
    language: str
    root: str  # thư mục con chứa file đánh dấu ("" = gốc repo)
    markers: list[str]
    tools: list[str]  # chương trình cần có trên máy
    commands: dict[str, list[str]] = field(
        default_factory=dict
    )  # build|lint|typecheck|test → ứng viên theo ưu tiên
    restore: list[str] = field(default_factory=list)  # cài phụ thuộc (ddt onboard --install, pkg.restore)


WINGET = {
    "python": "winget install Python.Python.3.12",
    "node": "winget install OpenJS.NodeJS.LTS",
    "go": "winget install GoLang.Go",
    "dotnet": "winget install Microsoft.DotNet.SDK.8",
    "java": "winget install EclipseAdoptium.Temurin.21.JDK",
    "mvn": "install Apache Maven (https://maven.apache.org/download.cgi) and add it to PATH",
    "gradle": "use the repo's gradlew wrapper, or install Gradle",
}


def _prefix(root: str, cmd: str) -> str:
    return f"cd {root} && {cmd}" if root else cmd


def detect_stacks(files: Sequence[str], read: Callable[[str], str | None]) -> list[Stack]:
    """files: đường dẫn POSIX (git ls-files). read(rel) → nội dung text hoặc None."""
    by_name: dict[str, list[str]] = {}
    for f in files:
        by_name.setdefault(PurePosixPath(f).name, []).append(f)
    stacks: list[Stack] = []

    def roots(name: str) -> list[str]:
        out = sorted({str(PurePosixPath(p).parent) for p in by_name.get(name, [])}, key=len)
        out = [("" if r == "." else r) for r in out]
        return [r for r in out if "node_modules" not in r and "vendor" not in r][:3]

    # Python
    py_markers = ("pyproject.toml", "setup.py", "requirements.txt", "pytest.ini", "setup.cfg", "tox.ini")
    py_roots = next((roots(m) for m in py_markers if roots(m)), [])
    if not py_roots and any(f.endswith(".py") for f in files):
        py_roots = [""]
    for r in py_roots:
        text = (read(f"{r}/pyproject.toml" if r else "pyproject.toml") or "").lower()
        st = Stack("python", r, [m for m in py_markers if by_name.get(m)] or ["*.py"], ["python"])
        st.commands["test"] = [_prefix(r, "python -m pytest -q")]
        if "ruff" in text or shutil.which("ruff"):
            st.commands["lint"] = [_prefix(r, "ruff check .")]
        if "mypy" in text:
            st.commands["typecheck"] = [_prefix(r, "mypy .")]
        req = f"{r}/requirements.txt" if r else "requirements.txt"
        if req in files:
            st.restore.append(_prefix(r, "python -m pip install -r requirements.txt"))
        stacks.append(st)
    # JavaScript / TypeScript
    for r in roots("package.json"):
        pj = read(f"{r}/package.json" if r else "package.json") or "{}"
        try:
            scripts = (json.loads(pj).get("scripts") or {}) if pj.strip() else {}
        except json.JSONDecodeError:
            scripts = {}
        pm = "npm"
        lock = "package-lock.json"
        for name, tool in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn")):
            if any(p.startswith(r) for p in by_name.get(name, [])):
                pm, lock = tool, name
        st = Stack(
            "javascript", r, ["package.json", lock] if by_name.get(lock) else ["package.json"], ["node", pm]
        )
        if "test" in scripts:
            st.commands["test"] = [_prefix(r, f"{pm} test")]
        for key, script in (("lint", "lint"), ("build", "build"), ("typecheck", "typecheck")):
            if script in scripts:
                st.commands[key] = [_prefix(r, f"{pm} run {script}")]
        if "typecheck" not in st.commands and any(p.startswith(r) for p in by_name.get("tsconfig.json", [])):
            st.commands["typecheck"] = [_prefix(r, "npx --no-install tsc --noEmit")]
        has_deps = '"dependencies"' in pj or '"devDependencies"' in pj
        if has_deps:
            st.restore.append(
                _prefix(
                    r,
                    {
                        "npm": "npm ci --ignore-scripts",
                        "yarn": "yarn install --frozen-lockfile --ignore-scripts",
                        "pnpm": "pnpm install --frozen-lockfile --ignore-scripts",
                    }[pm],
                )
            )
        stacks.append(st)
    # Go
    for r in roots("go.mod"):
        st = Stack("go", r, ["go.mod"], ["go"])
        st.commands = {
            "test": [_prefix(r, "go test ./...")],
            "lint": [_prefix(r, "go vet ./...")],
            "build": [_prefix(r, "go build ./...")],
        }
        st.restore.append(_prefix(r, "go mod download"))
        stacks.append(st)
    # Java (Maven / Gradle)
    for r in roots("pom.xml"):
        wrapper = any(p.startswith(r) for p in by_name.get("mvnw", []))
        mvn = "./mvnw" if wrapper else "mvn"
        st = Stack("java", r, ["pom.xml"], ["java"] + ([] if wrapper else ["mvn"]))
        st.commands = {
            "test": [_prefix(r, f"{mvn} -q -o test"), _prefix(r, f"{mvn} -q test")],
            "build": [_prefix(r, f"{mvn} -q -o compile"), _prefix(r, f"{mvn} -q compile")],
        }
        st.restore.append(_prefix(r, f"{mvn} -q dependency:go-offline"))
        stacks.append(st)
    for r in roots("build.gradle") or roots("build.gradle.kts"):
        wrapper = any(p.startswith(r) for p in by_name.get("gradlew", []))
        gradle = "./gradlew" if wrapper else "gradle"
        st = Stack("java", r, ["build.gradle"], ["java"] + ([] if wrapper else ["gradle"]))
        st.commands = {
            "test": [_prefix(r, f"{gradle} test --offline"), _prefix(r, f"{gradle} test")],
            "build": [_prefix(r, f"{gradle} assemble --offline")],
        }
        stacks.append(st)
    # .NET
    sln = [f for f in files if f.endswith(".sln")]
    csproj = [f for f in files if f.endswith((".csproj", ".fsproj"))]
    if sln or csproj:
        st = Stack("dotnet", "", (sln or csproj)[:2], ["dotnet"])
        if sln:
            test_target = build_target = sln[0]
        else:  # không có .sln: dotnet cần đường dẫn project cụ thể
            tests = [c for c in csproj if "test" in c.lower()]
            test_target = (tests or csproj)[0]
            build_target = next((c for c in csproj if c not in tests), csproj[0])
        st.commands = {"test": [f"dotnet test {test_target}"], "build": [f"dotnet build {build_target}"]}
        st.restore.append(f"dotnet restore {test_target}")
        stacks.append(st)
    return stacks


def missing_tools(stacks: Sequence[Stack]) -> dict[str, str]:
    """Chương trình chưa có trên PATH → gợi ý cài (DDT không tự cài toolchain hệ thống)."""
    need = {t for s in stacks for t in s.tools}
    return {t: WINGET.get(t, f"install {t}") for t in sorted(need) if shutil.which(t) is None}
