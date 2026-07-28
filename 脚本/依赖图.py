#!/usr/bin/env python3
"""解析项目内部导入并输出依赖图、中心模块和循环依赖。"""

import ast
import json
import posixpath
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Optional, Set, Tuple

from 扫描 import (
    SOURCE_EXTENSIONS,
    iter_files,
    read_text_limited,
    read_text_with_status,
)


JS_IMPORT = re.compile(
    r"(?:import|export)\s+(?:[\s\S]*?\s+from\s+)?[\"']([^\"']+)[\"']|"
    r"require\s*\(\s*[\"']([^\"']+)[\"']\s*\)|"
    r"import\s*\(\s*[\"']([^\"']+)[\"']\s*\)"
)
RUST_IMPORT = re.compile(r"^\s*use\s+((?:crate|self|super)::[A-Za-z0-9_:]+)", re.MULTILINE)
GO_IMPORT = re.compile(r"(?:^|\n)\s*import\s+(?:\(\s*([\s\S]*?)\s*\)|(?:[A-Za-z_.]+\s+)?[\"']([^\"']+)[\"'])")
GO_QUOTED = re.compile(r"[\"']([^\"']+)[\"']")
SUPPORTED_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go"}
Alias = Tuple[str, str, str]


def _extract_imports(path: Path, content: str) -> Iterable[str]:
    if path.suffix == ".py":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                prefix = "." * node.level
                if node.module:
                    imports.append(prefix + node.module)
                    imports.extend(
                        prefix + node.module + "." + alias.name
                        for alias in node.names if alias.name != "*"
                    )
                else:
                    imports.extend(prefix + alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
        return imports
    if path.suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return [next(group for group in match.groups() if group) for match in JS_IMPORT.finditer(content)]
    if path.suffix == ".rs":
        return [match.group(1) for match in RUST_IMPORT.finditer(content)]
    if path.suffix == ".go":
        imports = []
        for match in GO_IMPORT.finditer(content):
            if match.group(2):
                imports.append(match.group(2))
            else:
                imports.extend(GO_QUOTED.findall(match.group(1) or ""))
        return imports
    return []


def _strip_jsonc(content: str) -> str:
    output = []
    index = 0
    in_string = False
    escaped = False
    while index < len(content):
        char = content[index]
        next_char = content[index + 1] if index + 1 < len(content) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
        elif char == "/" and next_char == "/":
            index += 2
            while index < len(content) and content[index] not in "\r\n":
                index += 1
        elif char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(content) and content[index:index + 2] != "*/":
                index += 1
            index += 2
        else:
            output.append(char)
            index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(output))


def _load_jsonc(path: Path) -> Tuple[Dict[str, object], Optional[str]]:
    content = read_text_limited(path, 500_000)
    if not content:
        return {}, "empty_or_unreadable"
    try:
        config = json.loads(_strip_jsonc(content))
    except (TypeError, ValueError):
        return {}, "invalid_jsonc"
    if not isinstance(config, dict):
        return {}, "root_not_object"
    return config, None


def _tsconfig_chain(path: Path, root: Path, seen: Set[Path]) -> List[Path]:
    path = path.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return []
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    config, _ = _load_jsonc(path)
    chain = []
    parent = config.get("extends")
    if isinstance(parent, str) and parent.startswith((".", "/")):
        parent_path = (path.parent / parent).resolve()
        json_candidate = Path(str(parent_path) + ".json")
        if not parent_path.is_file() and json_candidate.is_file():
            parent_path = json_candidate
        chain.extend(_tsconfig_chain(parent_path, root, seen))
    chain.append(path)
    return chain


def _load_ts_aliases(root: Path) -> Tuple[List[Alias], List[Dict[str, str]]]:
    aliases: List[Alias] = []
    errors: List[Dict[str, str]] = []
    configs = sorted(
        path for path in iter_files(root)
        if path.name == "tsconfig.json" or (
            path.name.startswith("tsconfig.") and path.suffix == ".json"
        )
    )
    for entry in configs:
        scope_path = entry.parent.relative_to(root).as_posix()
        scope = "" if scope_path == "." else scope_path
        entry_config, entry_error = _load_jsonc(entry)
        if entry_error:
            errors.append({
                "file": entry.relative_to(root).as_posix(),
                "reason": entry_error,
            })
            continue
        elif isinstance(entry_config.get("extends"), str) and not str(
            entry_config["extends"]
        ).startswith((".", "/")):
            errors.append({
                "file": entry.relative_to(root).as_posix(),
                "reason": "package_extends_unsupported",
            })
        for path in _tsconfig_chain(entry, root, set()):
            config, error = _load_jsonc(path)
            if error:
                errors.append({
                    "file": path.relative_to(root).as_posix()
                    if path.is_relative_to(root) else str(path),
                    "reason": error,
                })
                continue
            compiler = config.get("compilerOptions", {})
            if not isinstance(compiler, dict):
                continue
            base_url = str(compiler.get("baseUrl", "."))
            paths = compiler.get("paths", {})
            if not isinstance(paths, dict):
                continue
            for alias, targets in paths.items():
                if not isinstance(targets, list):
                    continue
                for target in targets:
                    resolved_target = path.parent / base_url / str(target)
                    try:
                        relative_target = resolved_target.resolve().relative_to(root)
                    except ValueError:
                        continue
                    aliases.append((str(alias), relative_target.as_posix(), scope))
    aliases.reverse()
    aliases.sort(key=lambda item: len(Path(item[2]).parts), reverse=True)
    return aliases, errors


def _apply_alias(imported: str, source: str, aliases: List[Alias]) -> Optional[str]:
    for alias, target, scope in aliases:
        if scope and source != scope and not source.startswith(scope + "/"):
            continue
        if "*" in alias:
            prefix, suffix = alias.split("*", 1)
            if imported.startswith(prefix) and imported.endswith(suffix):
                middle = imported[len(prefix):len(imported) - len(suffix) if suffix else None]
                return target.replace("*", middle)
        elif imported == alias:
            return target
    return None


def _candidates(
    source: str,
    imported: str,
    aliases: List[Alias],
    go_module: str,
) -> List[str]:
    source_path = Path(source)
    bases: List[Path]
    if imported.startswith(".") and source_path.suffix != ".py":
        bases = [source_path.parent / imported]
    elif source_path.suffix == ".py" and imported.startswith("."):
        levels = len(imported) - len(imported.lstrip("."))
        module = imported.lstrip(".").replace(".", "/")
        base_dir = source_path.parent
        for _ in range(max(0, levels - 1)):
            base_dir = base_dir.parent
        bases = [base_dir / module]
    elif source_path.suffix == ".py" and imported:
        module_path = Path(imported.replace(".", "/"))
        bases = [module_path, source_path.parent / module_path]
    elif source_path.suffix in {".js", ".jsx", ".ts", ".tsx"}:
        aliased = _apply_alias(imported, source, aliases)
        if aliased:
            bases = [Path(aliased)]
        elif imported.startswith(("@/", "~/")):
            bases = [Path("src") / imported[2:]]
        else:
            return []
    elif source_path.suffix == ".rs":
        prefix, module = imported.split("::", 1)
        module_path = Path(module.replace("::", "/"))
        if prefix == "crate":
            bases = [Path("src") / module_path]
        elif prefix == "self":
            bases = [source_path.parent / module_path]
        else:
            bases = [source_path.parent.parent / module_path]
    elif source_path.suffix == ".go" and go_module and imported.startswith(go_module + "/"):
        bases = [Path(imported[len(go_module) + 1:])]
    else:
        return []

    candidates = []
    for base in bases:
        normalized = Path(*[part for part in base.parts if part != "."])
        raw = normalized.as_posix()
        candidates.append(raw)
        for ext in SOURCE_EXTENSIONS:
            candidates.append(raw + ext)
            candidates.append((normalized / ("index" + ext)).as_posix())
            candidates.append((normalized / ("mod" + ext)).as_posix())
    return candidates


def _resolve(
    source: str,
    imported: str,
    nodes: Set[str],
    aliases: List[Alias],
    go_module: str,
) -> Optional[str]:
    for candidate in _candidates(source, imported, aliases, go_module):
        # Path.resolve is intentionally avoided: candidates are project-relative.
        clean = posixpath.normpath(Path(candidate).as_posix())
        if clean == ".." or clean.startswith("../"):
            continue
        if clean in nodes:
            return clean
    return None


def _cycles(edges: Dict[str, Set[str]]) -> List[List[str]]:
    found: Set[Tuple[str, ...]] = set()
    visiting: Set[str] = set()
    visited: Set[str] = set()
    stack: List[str] = []

    def visit(node: str) -> None:
        if node in visiting:
            start = stack.index(node)
            cycle = stack[start:] + [node]
            body = cycle[:-1]
            rotations = [tuple(body[i:] + body[:i]) for i in range(len(body))]
            found.add(min(rotations) + (min(rotations)[0],))
            return
        if node in visited:
            return
        visiting.add(node)
        stack.append(node)
        for target in edges.get(node, set()):
            visit(target)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)
    return [list(cycle) for cycle in sorted(found)]


def build_graph(root: str) -> Dict[str, object]:
    project_root = Path(root).expanduser().resolve()
    if not project_root.exists() or not project_root.is_dir():
        return {"error": "路径不是有效目录：{}".format(project_root)}

    all_source_files = [p for p in iter_files(project_root) if p.suffix in SOURCE_EXTENSIONS]
    files = [p for p in all_source_files if p.suffix in SUPPORTED_EXTENSIONS]
    nodes = {p.relative_to(project_root).as_posix() for p in files}
    raw_imports: Dict[str, Iterable[str]] = {}
    skipped_files = []
    for path in files:
        rel = path.relative_to(project_root).as_posix()
        content, error = read_text_with_status(path)
        if error:
            skipped_files.append({"file": rel, "reason": error})
            continue
        raw_imports[rel] = _extract_imports(path, content)

    aliases, tsconfig_errors = _load_ts_aliases(project_root)
    go_mod = read_text_limited(project_root / "go.mod", 500_000)
    module_match = re.search(r"^\s*module\s+(\S+)", go_mod, re.MULTILINE)
    go_module = module_match.group(1) if module_match else ""
    edges: DefaultDict[str, Set[str]] = defaultdict(set)
    package_edges: Set[Tuple[str, str]] = set()
    go_packages = {
        Path(node).parent.as_posix()
        for node in nodes if node.endswith(".go")
    }
    unresolved = 0
    for source, imports in raw_imports.items():
        for imported in imports:
            if source.endswith(".go") and go_module and imported.startswith(go_module + "/"):
                package_target = imported[len(go_module) + 1:]
                if package_target in go_packages:
                    package_edges.add((source, package_target))
                else:
                    unresolved += 1
                continue
            target = _resolve(source, imported, nodes, aliases, go_module)
            if target and target != source:
                edges[source].add(target)
            elif imported.startswith((".", "@/", "~/")):
                unresolved += 1

    incoming = {node: 0 for node in nodes}
    for targets in edges.values():
        for target in targets:
            incoming[target] += 1
    return {
        "schema_version": "2.0",
        "total_files_analyzed": len(nodes),
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "unsupported_source_files": [
            path.relative_to(project_root).as_posix()
            for path in all_source_files if path.suffix not in SUPPORTED_EXTENSIONS
        ],
        "total_import_edges": sum(map(len, edges.values())),
        "unresolved_local_imports": unresolved,
        "edges": [
            {"source": source, "target": target}
            for source in sorted(edges) for target in sorted(edges[source])
        ],
        "package_edges": [
            {"source": source, "target_package": target}
            for source, target in sorted(package_edges)
        ],
        "most_imported_modules": [
            {"module": node, "imported_by_count": count}
            for node, count in sorted(incoming.items(), key=lambda item: (-item[1], item[0]))
            if count > 0
        ][:20],
        "leaf_modules": sorted(
            node for node in nodes if incoming[node] == 0 and edges.get(node)
        )[:20],
        "modules_with_most_deps": [
            {"module": node, "dependency_count": len(targets)}
            for node, targets in sorted(edges.items(), key=lambda item: (-len(item[1]), item[0]))
        ][:20],
        "cycles": _cycles(edges),
        "diagnostics": {
            "skipped_files": skipped_files,
            "tsconfig_errors": tsconfig_errors,
        },
    }


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    result = build_graph(path)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    if "error" in result:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
