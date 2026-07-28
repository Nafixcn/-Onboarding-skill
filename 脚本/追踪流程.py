#!/usr/bin/env python3
"""从符号定义出发，静态追踪直接调用关系并输出文件与行号。"""

import argparse
import ast
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Set, Tuple

from 依赖图 import build_graph
from 扫描 import SOURCE_EXTENSIONS, iter_files, read_text_limited


GENERIC_DEFINITION = re.compile(
    r"\b(?:function|class|fn|func)\s+([A-Za-z_$][\w$]*)|"
    r"\b(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
)
GENERIC_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
CALL_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "function", "return", "new",
    "super", "typeof", "sizeof", "print",
}


def _python_symbols(path: Path, rel: str, content: str) -> List[Dict[str, object]]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    class DirectCallVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.calls: List[str] = []

        def visit_Call(self, call: ast.Call) -> None:
            target = call.func
            if isinstance(target, ast.Name):
                self.calls.append(target.id)
            elif isinstance(target, ast.Attribute):
                self.calls.append(target.attr)
            self.generic_visit(call)

        def visit_FunctionDef(self, definition: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, definition: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, definition: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, expression: ast.Lambda) -> None:
            return

    symbols = []
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        visitor = DirectCallVisitor()
        if not isinstance(node, ast.ClassDef):
            for statement in node.body:
                visitor.visit(statement)
        line = content.splitlines()[node.lineno - 1].strip()
        qualifiers = [node.name]
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualifiers.append(parent.name)
            parent = parents.get(parent)
        symbols.append({
            "name": node.name, "file": rel, "line": node.lineno,
            "qualname": ".".join(reversed(qualifiers)),
            "content": line, "calls": sorted(set(visitor.calls) - {node.name}),
            "confidence": "high",
        })
    return symbols


def _generic_symbols(rel: str, content: str) -> List[Dict[str, object]]:
    lines = content.splitlines()
    symbols = []
    for index, line in enumerate(lines):
        match = GENERIC_DEFINITION.search(line)
        if not match:
            continue
        name = match.group(1) or match.group(2)
        remaining = "\n".join(lines[index:min(len(lines), index + 200)])
        brace_start = remaining.find("{")
        if brace_start >= 0:
            depth = 0
            end = len(remaining)
            for position, character in enumerate(remaining[brace_start:], brace_start):
                if character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth == 0:
                        end = position + 1
                        break
            window = remaining[:end]
        else:
            window = line
        calls = {
            call for call in GENERIC_CALL.findall(window)
            if call not in CALL_KEYWORDS and call != name
        }
        symbols.append({
            "name": name, "file": rel, "line": index + 1,
            "qualname": name,
            "content": line.strip(), "calls": sorted(calls),
            "confidence": "heuristic",
        })
    return symbols


def build_symbol_index(project_root: Path) -> Tuple[DefaultDict[str, List[Dict[str, object]]], List[Dict[str, object]]]:
    project_root = project_root.resolve()
    by_name: DefaultDict[str, List[Dict[str, object]]] = defaultdict(list)
    all_symbols = []
    for path in iter_files(project_root):
        if path.suffix not in SOURCE_EXTENSIONS:
            continue
        rel = path.relative_to(project_root).as_posix()
        try:
            content = read_text_limited(path)
        except OSError:
            continue
        if not content:
            continue
        symbols = _python_symbols(path, rel, content) if path.suffix == ".py" else _generic_symbols(rel, content)
        for symbol in symbols:
            by_name[str(symbol["name"])].append(symbol)
            all_symbols.append(symbol)
    return by_name, all_symbols


def find_definition(project_root: Path, search_term: str) -> List[Dict[str, object]]:
    index, _ = build_symbol_index(project_root)
    return index.get(search_term, [])


def find_references(project_root: Path, search_term: str) -> List[Dict[str, object]]:
    project_root = project_root.resolve()
    results = []
    token = re.compile(r"\b{}\b".format(re.escape(search_term)))
    for path in iter_files(project_root):
        if path.suffix not in SOURCE_EXTENSIONS:
            continue
        try:
            content = read_text_limited(path)
        except OSError:
            continue
        if not content:
            continue
        lines = content.splitlines()
        for line_no, line in enumerate(lines, 1):
            if token.search(line):
                results.append({
                    "file": path.relative_to(project_root).as_posix(),
                    "line": line_no, "content": line.strip(),
                })
    return results[:100]


def _symbol_key(symbol: Dict[str, object]) -> Tuple[str, str, int]:
    return (
        str(symbol["file"]),
        str(symbol["qualname"]),
        int(symbol["line"]),
    )


def _resolve_call(
    name: str,
    caller_file: str,
    index: DefaultDict[str, List[Dict[str, object]]],
    dependency_targets: Dict[str, Set[str]],
) -> List[Dict[str, object]]:
    definitions = index.get(name, [])
    same_file = [item for item in definitions if item["file"] == caller_file]
    if same_file:
        return same_file
    imported_files = dependency_targets.get(caller_file, set())
    imported = [item for item in definitions if item["file"] in imported_files]
    if imported:
        return imported
    return definitions if len(definitions) == 1 else []


def trace_flow(project_root: Path, entry_point: str, max_depth: int = 5) -> str:
    project_root = project_root.resolve()
    index, _ = build_symbol_index(project_root)
    graph = build_graph(str(project_root))
    output = ["## 流程追踪：`{}`".format(entry_point), ""]
    unsupported = graph.get("unsupported_source_files", [])
    skipped = graph.get("diagnostics", {}).get("skipped_files", [])
    if unsupported:
        output.extend([
            "> 覆盖提示：{} 个源码文件属于未支持语言。".format(len(unsupported)),
            "",
        ])
    if skipped:
        output.extend([
            "> 覆盖提示：{} 个源码文件因读取限制被跳过。".format(len(skipped)),
            "",
        ])
    dependency_targets: DefaultDict[str, Set[str]] = defaultdict(set)
    for edge in graph.get("edges", []):
        dependency_targets[str(edge["source"])].add(str(edge["target"]))

    entry_definitions = index.get(entry_point, [])
    if not entry_definitions:
        refs = find_references(project_root, entry_point)
        if refs:
            for ref in refs[:5]:
                output.extend([
                    "▶ `{}:{}` — {}".format(
                        ref["file"], ref["line"], str(ref["content"])[:120]
                    ), ""
                ])
        else:
            output.extend(["▶ （未找到匹配 `{}`）".format(entry_point), ""])
        return "\n".join(output)

    queue = deque((symbol, 0, None) for symbol in entry_definitions[:5])
    expanded: Set[Tuple[str, str, int]] = set()
    emitted: Set[Tuple[str, str, int]] = set()

    while queue:
        symbol, depth, caller = queue.popleft()
        if depth > max_depth:
            continue
        indent = "  " * depth
        arrow = "▶" if depth == 0 else "→"
        key = _symbol_key(symbol)
        if key not in emitted:
            emitted.add(key)
            relation = "（由 `{}` 调用）".format(caller) if caller else ""
            output.extend([
                "{}{} `{}:{}` `{}` [{}] — {} {}".format(
                    indent, arrow, symbol["file"], symbol["line"],
                    symbol["qualname"], symbol["confidence"],
                    str(symbol["content"])[:120], relation
                ).rstrip(),
                "",
            ])
        if key in expanded or depth == max_depth:
            continue
        expanded.add(key)
        for call in symbol["calls"]:
            targets = _resolve_call(
                str(call), str(symbol["file"]), index, dependency_targets
            )
            for target in targets[:5]:
                queue.append((target, depth + 1, str(symbol["qualname"])))

    return "\n".join(output)


def main() -> None:
    parser = argparse.ArgumentParser(description="追踪代码库中的静态调用流程")
    parser.add_argument("project_path", nargs="?", default=".", help="项目根目录路径")
    parser.add_argument("search_term", nargs="?", help="函数、类或导出符号名称")
    parser.add_argument("--depth", type=int, default=5, help="最大追踪深度（非负整数）")
    args = parser.parse_args()
    root = Path(args.project_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        parser.error("项目路径不是有效目录：{}".format(root))
    if not args.search_term:
        parser.error("请提供要追踪的搜索词")
    if args.depth < 0:
        parser.error("--depth 必须是非负整数")
    print(trace_flow(root, args.search_term, args.depth))


if __name__ == "__main__":
    main()
