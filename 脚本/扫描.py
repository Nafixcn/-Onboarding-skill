#!/usr/bin/env python3
"""生成代码库的结构化 JSON 概览（仅使用 Python 标准库）。"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


TECH_MANIFESTS = {
    "package.json": "Node.js / JavaScript / TypeScript",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "go.work": "Go workspace",
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "requirements.txt": "Python",
    "Gemfile": "Ruby",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java / Kotlin (Gradle)",
    "build.gradle.kts": "Java / Kotlin (Gradle Kotlin DSL)",
    "composer.json": "PHP",
    "mix.exs": "Elixir",
    "CMakeLists.txt": "C / C++ (CMake)",
    "Makefile": "Make",
}

FRAMEWORK_PATTERNS = {
    "react": "React", "vue": "Vue.js", "next": "Next.js", "nuxt": "Nuxt.js",
    "svelte": "Svelte", "@angular/core": "Angular", "express": "Express.js",
    "fastify": "Fastify", "koa": "Koa", "@nestjs/core": "NestJS",
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI",
    "spring-boot": "Spring Boot", "gin-gonic/gin": "Gin (Go)",
    "labstack/echo": "Echo (Go)", "actix-web": "Actix (Rust)",
    "axum": "Axum (Rust)", "rocket": "Rocket (Rust)", "laravel": "Laravel",
    "rails": "Ruby on Rails",
}

COMMON_DIRS = {
    "src": "应用源码", "lib": "库代码", "components": "UI 组件",
    "pages": "页面级组件 / 路由处理", "services": "业务逻辑层",
    "utils": "共享工具函数", "helpers": "辅助函数", "hooks": "自定义 Hooks",
    "api": "API 客户端 / 服务端路由", "routes": "路由定义",
    "models": "数据模型 / ORM 实体", "entities": "数据库实体",
    "config": "配置文件", "stores": "状态管理", "store": "状态管理",
    "middleware": "中间件", "types": "类型定义", "assets": "静态资源",
    "styles": "样式表", "public": "公共静态文件", "static": "静态文件",
    "tests": "测试文件", "__tests__": "测试文件", "test": "测试文件",
    "spec": "测试规范", "docs": "文档", "scripts": "构建 / CI 脚本",
    "migrations": "数据库迁移", "fixtures": "测试 Fixtures",
    "docker": "Docker 配置", "packages": "Monorepo 包", "apps": "Monorepo 应用",
}

DEFAULT_IGNORES = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "__pycache__", ".venv",
    "venv", "dist", "build", "target", "coverage", ".next", ".cache",
}
ALLOWED_HIDDEN_DIRS = {".github"}
MAX_SOURCE_BYTES = 2 * 1024 * 1024
SOURCE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".py", ".rs", ".go", ".java", ".kt",
    ".rb", ".php", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp",
}
ENTRY_POINT_NAMES = {
    "main.ts", "main.tsx", "main.js", "main.jsx", "index.ts", "index.tsx",
    "index.js", "index.jsx", "app.ts", "app.tsx", "app.js", "app.jsx",
    "server.ts", "server.js", "main.go", "__init__.py", "main.py", "app.py",
    "manage.py", "lib.rs", "main.rs",
}
CONFIG_NAMES = {
    "Dockerfile", "Makefile", "tsconfig.json", "vite.config.ts",
    "vite.config.js", "webpack.config.js", "webpack.config.ts",
    ".gitlab-ci.yml", "Jenkinsfile",
}


def _gitignore_patterns(root: Path) -> List[Tuple[str, bool]]:
    path = root / ".gitignore"
    if not path.is_file():
        return []
    try:
        patterns = []
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            patterns.append((line.lstrip("!/"), negated))
        return patterns
    except OSError:
        return []


def _ignored(rel: Path, patterns: Iterable[Tuple[str, bool]]) -> bool:
    text = rel.as_posix()
    ignored = False
    for pattern, negated in patterns:
        clean = pattern.rstrip("/")
        if rel.match(clean) or rel.match(clean + "/**") or text == clean:
            ignored = not negated
    return ignored


def _git_files(root: Path) -> Optional[List[Path]]:
    if not (root / ".git").exists():
        return None
    safe_git = [
        "git", "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.untrackedCache=false",
        "-C", str(root),
    ]
    try:
        top_level = subprocess.run(
            safe_git + ["rev-parse", "--show-toplevel"],
            capture_output=True,
            check=True,
            timeout=10,
            text=True,
        ).stdout.strip()
        if Path(top_level).resolve() != root:
            return None
        process = subprocess.run(
            safe_git + [
                "ls-files", "--cached", "--others", "--exclude-standard", "-z"
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    paths = []
    for raw in process.stdout.split(b"\0"):
        if not raw:
            continue
        rel = Path(raw.decode("utf-8", errors="surrogateescape"))
        path = root / rel
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    return sorted(paths)


def _walk_files(root: Path) -> List[Path]:
    root = root.resolve()
    patterns = _gitignore_patterns(root)
    result = []
    for current, dirs, files in os.walk(str(root), followlinks=False):
        current_path = Path(current)
        kept = []
        for name in dirs:
            candidate = current_path / name
            rel = candidate.relative_to(root)
            hidden_allowed = not name.startswith(".") or name in ALLOWED_HIDDEN_DIRS
            if (not candidate.is_symlink() and name not in DEFAULT_IGNORES
                    and hidden_allowed and not _ignored(rel, patterns)):
                kept.append(name)
        dirs[:] = kept
        for name in files:
            path = current_path / name
            rel = path.relative_to(root)
            if path.is_symlink() or _ignored(rel, patterns):
                continue
            try:
                path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            if path.is_file():
                result.append(path)
    return result


def project_files(root: Path) -> Tuple[List[Path], str]:
    """返回可分析文件以及枚举来源：git 或 fallback。"""
    root = root.resolve()
    git_files = _git_files(root)
    if git_files is not None:
        return git_files, "git"
    return _walk_files(root), "fallback"


def iter_files(root: Path) -> Iterable[Path]:
    files, _ = project_files(root)
    return iter(files)


def read_text_with_status(
    path: Path, limit: int = MAX_SOURCE_BYTES
) -> Tuple[str, Optional[str]]:
    try:
        if path.is_symlink():
            return "", "symlink"
        if not path.is_file():
            return "", "not_file"
        size = path.stat().st_size
        if size > limit:
            return "", "size_limit:{}>{}".format(size, limit)
        return path.read_text(encoding="utf-8", errors="strict"), None
    except UnicodeDecodeError:
        return "", "decode_error"
    except OSError as error:
        return "", "read_error:{}".format(type(error).__name__)


def read_text_limited(path: Path, limit: int = MAX_SOURCE_BYTES) -> str:
    """读取有大小上限的普通文件；超限、链接或错误均返回空字符串。"""
    content, _ = read_text_with_status(path, limit)
    return content


def _read_manifest(path: Path) -> str:
    return read_text_limited(path, 500_000)


def _detect_frameworks(path: Path, content: str) -> Set[str]:
    tokens: Set[str] = set()
    if path.name == "package.json":
        try:
            manifest = json.loads(content)
        except (TypeError, ValueError):
            manifest = {}
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            dependencies = manifest.get(section, {})
            if isinstance(dependencies, dict):
                tokens.update(str(name).lower() for name in dependencies)
    else:
        lowered = content.lower()
        for token in FRAMEWORK_PATTERNS:
            boundary = r"(?<![A-Za-z0-9_-]){}(?![A-Za-z0-9_-])".format(re.escape(token))
            if re.search(boundary, lowered):
                tokens.add(token)
    return {FRAMEWORK_PATTERNS[token] for token in tokens if token in FRAMEWORK_PATTERNS}


def _package_metadata(path: Path, content: str) -> Dict[str, object]:
    if path.name != "package.json":
        return {}
    try:
        manifest = json.loads(content)
    except (TypeError, ValueError):
        return {}
    result: Dict[str, object] = {}
    for key in ("name", "version", "private"):
        if key in manifest:
            result[key] = manifest[key]
    scripts = manifest.get("scripts")
    if isinstance(scripts, dict):
        result["scripts"] = dict(sorted(scripts.items()))
    return result


def _is_monorepo(root: Path) -> bool:
    if (root / "pnpm-workspace.yaml").is_file() or (root / "go.work").is_file():
        return True
    package_content = _read_manifest(root / "package.json")
    if package_content:
        try:
            package = json.loads(package_content)
            if package.get("workspaces"):
                return True
        except (TypeError, ValueError):
            pass
    cargo_content = _read_manifest(root / "Cargo.toml")
    if re.search(r"(?m)^\s*\[workspace\]\s*$", cargo_content):
        return True
    return False


def scan_project(root: str) -> Dict[str, object]:
    project_root = Path(root).expanduser().resolve()
    result: Dict[str, object] = {
        "schema_version": "2.0",
        "project_name": project_root.name, "project_path": str(project_root),
        "tech_stack": [], "frameworks": [], "entry_points": [],
        "build_and_ci": [], "directory_map": [], "file_stats": {},
        "manifest_details": [], "diagnostics": {
            "file_enumeration": "", "skipped_files": [],
        },
    }
    if not project_root.exists():
        result["error"] = "路径不存在：{}".format(project_root)
        return result
    if not project_root.is_dir():
        result["error"] = "路径不是目录：{}".format(project_root)
        return result

    files, enumeration = project_files(project_root)
    result["diagnostics"]["file_enumeration"] = enumeration
    top_dirs = {
        path.relative_to(project_root).parts[0]
        for path in files if len(path.relative_to(project_root).parts) > 1
        and not path.relative_to(project_root).parts[0].startswith(".")
    }
    manifests = []
    framework_names: Set[str] = set()
    file_counts = defaultdict(int)
    entries: Set[str] = set()
    configs: Set[str] = set()

    for path in files:
        rel = path.relative_to(project_root)
        file_counts[path.suffix.lower()] += 1
        if path.name in TECH_MANIFESTS and len(rel.parts) <= 4:
            manifests.append({"file": rel.as_posix(), "label": TECH_MANIFESTS[path.name]})
            content, error = read_text_with_status(path, 500_000)
            if error:
                result["diagnostics"]["skipped_files"].append({
                    "file": rel.as_posix(), "reason": error,
                })
            else:
                framework_names.update(_detect_frameworks(path, content))
                metadata = _package_metadata(path, content)
                if metadata:
                    result["manifest_details"].append({
                        "file": rel.as_posix(), **metadata,
                    })
        if path.name in ENTRY_POINT_NAMES and len(rel.parts) <= 4:
            entries.add(rel.as_posix())
        if path.name in CONFIG_NAMES or rel.as_posix().startswith(".github/workflows/"):
            configs.add(rel.as_posix())
        if path.name in {".env.example", ".env.template"}:
            configs.add(rel.as_posix())

    result["tech_stack"] = sorted(manifests, key=lambda item: item["file"])
    result["frameworks"] = sorted(framework_names)
    result["entry_points"] = sorted(entries)
    result["build_and_ci"] = sorted(configs)
    result["directory_map"] = [
        {"path": name, "description": COMMON_DIRS.get(name.lower(), "")}
        for name in sorted(top_dirs)
    ]
    result["file_stats"] = dict(sorted(file_counts.items(), key=lambda item: item[1], reverse=True)[:15])
    result["is_monorepo"] = _is_monorepo(project_root)
    return result


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    result = scan_project(path)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    if "error" in result:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
