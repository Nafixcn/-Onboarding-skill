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

from 扫描 import SOURCE_EXTENSIONS, iter_files, read_text_limited


JS_IMPORT = re.compile(
    r"(?:import|export)\s+(?:[\s\S]*?\s+from\s+)?[\"']([^\"']+)[\"']|"
    r"require\s*\(\s*[\"']([^\"']+)[\"']\s*\)|"
    r"import\s*\(\s*[\"']([^\"']+)[\"']\s*\)"
)
RUST_IMPORT = re.compile(r"^\s*use\s+((?:crate|self|super)::[A-Za-z0-9_:]+)", re.MULTILINE)
GO_IMPORT = re.compile(r"(?:^|\n)\s*import\s+(?:\(\s*([\s\S]*?)\s*\)|(?:[A-Za-z_.]+\s+)?[\"']([^\"']+)[\"'])")
GO_QUOTED = re.compile(r"[\"']([^\"']+)[\"']")


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


def _load_ts_aliases(root: Path) -> List[Tuple[str, str]]:
    path = root / "tsconfig.json"
    content = read_text_limited(path, 500_000)
    if not content:
        return []
    try:
        config = json.loads(content)
    except (TypeError, ValueError):
        return []
    compiler = config.get("compilerOptions", {})
    base_url = str(compiler.get("baseUrl", "."))
    aliases = []
    for alias, targets in compiler.get("paths", {}).items():
        if not isinstance(targets, list):
            continue
        for target in targets:
            aliases.append((alias, str(Path(base_url) / target)))
    return aliases


def _apply_alias(imported: str, aliases: List[Tuple[str, str]]) -> Optional[str]:
    for alias, target in aliases:
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
    aliases: List[Tuple[str, str]],
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
        aliased = _apply_alias(imported, aliases)
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
    aliases: List[Tuple[str, str]],
    go_module: str,
) -> Optional[str]:
    for candidate in _candidates(source, imported, aliases, go_module):
        # Path.resolve is intentionally avoided: candidates are project-relative.
        clean = posixpath.normpath(Path(candidate).as_posix())
        if clean == ".." or clean.startswith("../"):
            continue
        if clean in nodes:
            return clean
        if source.endswith(".go"):
            package_files = sorted(
                node for node in nodes
                if node.startswith(clean.rstrip("/") + "/")
                and node.endswith(".go") and not node.endswith("_test.go")
            )
            if package_files:
                return package_files[0]
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

    files = [p for p in iter_files(project_root) if p.suffix in SOURCE_EXTENSIONS]
    nodes = {p.relative_to(project_root).as_posix() for p in files}
    raw_imports: Dict[str, Iterable[str]] = {}
    for path in files:
        rel = path.relative_to(project_root).as_posix()
        try:
            content = read_text_limited(path)
        except OSError:
            continue
        if not content:
            continue
        raw_imports[rel] = _extract_imports(path, content)

    aliases = _load_ts_aliases(project_root)
    go_mod = read_text_limited(project_root / "go.mod", 500_000)
    module_match = re.search(r"^\s*module\s+(\S+)", go_mod, re.MULTILINE)
    go_module = module_match.group(1) if module_match else ""
    edges: DefaultDict[str, Set[str]] = defaultdict(set)
    unresolved = 0
    for source, imports in raw_imports.items():
        for imported in imports:
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
        "total_files_analyzed": len(nodes),
        "total_import_edges": sum(map(len, edges.values())),
        "unresolved_local_imports": unresolved,
        "edges": [
            {"source": source, "target": target}
            for source in sorted(edges) for target in sorted(edges[source])
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
