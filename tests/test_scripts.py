import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "脚本"
sys.path.insert(0, str(SCRIPTS))

from 扫描 import scan_project
from 依赖图 import build_graph
from 追踪流程 import trace_flow


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_scanner_detects_nested_entry_framework_and_ignores(self):
        self.write("package.json", '{"dependencies":{"react":"1.0.0"}}')
        self.write("src/main.ts", "import React from 'react'")
        self.write("node_modules/pkg/index.js", "ignored")
        result = scan_project(str(self.root))
        self.assertEqual("2.0", result["schema_version"])
        self.assertEqual(["React"], result["frameworks"])
        self.assertIn("src/main.ts", result["entry_points"])
        self.assertEqual(1, result["file_stats"][".ts"])

    def test_scanner_does_not_treat_preact_as_react(self):
        self.write("package.json", '{"dependencies":{"preact":"1.0.0"}}')
        result = scan_project(str(self.root))
        self.assertNotIn("React", result["frameworks"])

    def test_scanner_detects_dev_dependency_framework(self):
        self.write("package.json", '{"devDependencies":{"vue":"1.0.0"}}')
        result = scan_project(str(self.root))
        self.assertIn("Vue.js", result["frameworks"])

    def test_scanner_extracts_package_metadata(self):
        self.write(
            "package.json",
            '{"name":"demo","version":"1.2.3","scripts":{"test":"node test.js"}}',
        )
        result = scan_project(str(self.root))
        self.assertEqual("demo", result["manifest_details"][0]["name"])
        self.assertEqual("1.2.3", result["manifest_details"][0]["version"])
        self.assertEqual(
            "node test.js",
            result["manifest_details"][0]["scripts"]["test"],
        )

    def test_scanner_rejects_file(self):
        self.write("README.md", "x")
        result = scan_project(str(self.root / "README.md"))
        self.assertIn("error", result)

    def test_scanner_honors_gitignore_negation(self):
        self.write(".gitignore", "*.log\n!important.log\n")
        self.write("debug.log", "ignored")
        self.write("important.log", "kept")
        result = scan_project(str(self.root))
        self.assertEqual(1, result["file_stats"][".log"])

    def test_scanner_excludes_ignored_top_level_directory_from_map(self):
        self.write(".gitignore", "generated/\n")
        self.write("generated/output.py", "VALUE = 1")
        self.write("src/main.py", "VALUE = 2")
        result = scan_project(str(self.root))
        paths = {item["path"] for item in result["directory_map"]}
        self.assertNotIn("generated", paths)
        self.assertIn("src", paths)

    def test_scanner_uses_git_standard_ignore_rules_when_available(self):
        self.write(".gitignore", "ignored.py\n")
        self.write("ignored.py", "SECRET = 1")
        self.write("kept.py", "VALUE = 1")
        subprocess.run(
            ["git", "init", "-q", str(self.root)],
            check=True, capture_output=True,
        )
        result = scan_project(str(self.root))
        self.assertEqual("git", result["diagnostics"]["file_enumeration"])
        self.assertEqual(1, result["file_stats"][".py"])

    def test_scanner_reports_oversized_manifest(self):
        self.write("package.json", " " * 500_001)
        result = scan_project(str(self.root))
        self.assertEqual(1, len(result["diagnostics"]["skipped_files"]))
        self.assertIn(
            "size_limit",
            result["diagnostics"]["skipped_files"][0]["reason"],
        )

    def test_scanner_does_not_treat_nested_manifest_as_monorepo(self):
        self.write("examples/demo/package.json", '{"name":"demo"}')
        result = scan_project(str(self.root))
        self.assertFalse(result["is_monorepo"])

    def test_scanner_detects_declared_workspace(self):
        self.write(
            "package.json",
            '{"name":"workspace","workspaces":["packages/*"]}',
        )
        result = scan_project(str(self.root))
        self.assertTrue(result["is_monorepo"])

    def test_scanner_finds_github_workflow(self):
        self.write(".github/workflows/test.yml", "name: tests")
        result = scan_project(str(self.root))
        self.assertIn(".github/workflows/test.yml", result["build_and_ci"])

    def test_scanner_skips_symlink_files(self):
        outside = Path(self.temp.name).parent / (self.root.name + "-outside.py")
        outside.write_text("SECRET = 1", encoding="utf-8")
        try:
            os.symlink(str(outside), str(self.root / "leak.py"))
            result = scan_project(str(self.root))
            self.assertNotIn(".py", result["file_stats"])
        finally:
            outside.unlink(missing_ok=True)

    def test_dependency_graph_resolves_js_and_finds_cycle(self):
        self.write("src/a.ts", "import { b } from './b'; export const a = b();")
        self.write("src/b.ts", "import { a } from './a'; export const b = () => a;")
        result = build_graph(str(self.root))
        self.assertEqual("2.0", result["schema_version"])
        edge_pairs = {(e["source"], e["target"]) for e in result["edges"]}
        self.assertIn(("src/a.ts", "src/b.ts"), edge_pairs)
        self.assertIn(("src/b.ts", "src/a.ts"), edge_pairs)
        self.assertEqual(1, len(result["cycles"]))

    def test_dependency_graph_excludes_isolated_files_from_leaf_modules(self):
        self.write("src/isolated.ts", "export const value = 1;")
        result = build_graph(str(self.root))
        self.assertNotIn("src/isolated.ts", result["leaf_modules"])

    def test_dependency_graph_resolves_python_relative_import(self):
        self.write("pkg/a.py", "from . import b\n")
        self.write("pkg/b.py", "VALUE = 1\n")
        result = build_graph(str(self.root))
        self.assertIn(
            {"source": "pkg/a.py", "target": "pkg/b.py"},
            result["edges"],
        )

    def test_dependency_graph_resolves_python_absolute_import(self):
        self.write("pkg/service.py", "from pkg.utils import helper\n")
        self.write("pkg/utils.py", "def helper(): pass\n")
        result = build_graph(str(self.root))
        self.assertIn(
            {"source": "pkg/service.py", "target": "pkg/utils.py"},
            result["edges"],
        )

    def test_dependency_graph_resolves_python_sibling_top_level_import(self):
        self.write("scripts/main.py", "from helper import run\n")
        self.write("scripts/helper.py", "def run(): pass\n")
        result = build_graph(str(self.root))
        self.assertIn(
            {"source": "scripts/main.py", "target": "scripts/helper.py"},
            result["edges"],
        )

    def test_dependency_graph_resolves_tsconfig_alias(self):
        self.write(
            "tsconfig.json",
            '{"compilerOptions":{"baseUrl":".","paths":{"@app/*":["src/*"]}}}',
        )
        self.write("src/main.ts", "import { helper } from '@app/utils';")
        self.write("src/utils.ts", "export const helper = () => 1;")
        result = build_graph(str(self.root))
        self.assertIn(
            {"source": "src/main.ts", "target": "src/utils.ts"},
            result["edges"],
        )

    def test_dependency_graph_resolves_jsonc_extended_tsconfig(self):
        self.write(
            "tsconfig.base.json",
            '{\n// shared aliases\n"compilerOptions": {'
            '"baseUrl": ".", "paths": {"@shared/*": ["src/*"],},},}',
        )
        self.write("tsconfig.json", '{"extends":"./tsconfig.base.json"}')
        self.write("src/main.ts", "import { helper } from '@shared/utils';")
        self.write("src/utils.ts", "export const helper = () => 1;")
        result = build_graph(str(self.root))
        self.assertIn(
            {"source": "src/main.ts", "target": "src/utils.ts"},
            result["edges"],
        )

    def test_dependency_graph_reports_invalid_tsconfig(self):
        self.write("tsconfig.json", "{ invalid")
        self.write("src/main.ts", "export const value = 1;")
        result = build_graph(str(self.root))
        self.assertEqual(
            "invalid_jsonc",
            result["diagnostics"]["tsconfig_errors"][0]["reason"],
        )

    def test_dependency_graph_resolves_extensionless_tsconfig_extends(self):
        self.write(
            "tsconfig.base.json",
            '{"compilerOptions":{"paths":{"@base/*":["src/*"]}}}',
        )
        self.write("tsconfig.json", '{"extends":"./tsconfig.base"}')
        self.write("src/main.ts", "import { value } from '@base/value';")
        self.write("src/value.ts", "export const value = 1;")
        result = build_graph(str(self.root))
        self.assertIn(
            {"source": "src/main.ts", "target": "src/value.ts"},
            result["edges"],
        )

    def test_dependency_graph_reports_missing_go_package(self):
        self.write("go.mod", "module example.com/project\n")
        self.write("main.go", 'package main\nimport "example.com/project/missing"\n')
        result = build_graph(str(self.root))
        self.assertEqual([], result["package_edges"])
        self.assertEqual(1, result["unresolved_local_imports"])

    def test_dependency_graph_resolves_go_module_import(self):
        self.write("go.mod", "module example.com/project\n")
        self.write("main.go", 'package main\nimport "example.com/project/internal/auth"\n')
        self.write("internal/auth/auth.go", "package auth\n")
        result = build_graph(str(self.root))
        self.assertIn(
            {"source": "main.go", "target_package": "internal/auth"},
            result["package_edges"],
        )

    def test_dependency_graph_reports_unsupported_source_languages(self):
        self.write("src/Main.java", "import app.Helper;")
        result = build_graph(str(self.root))
        self.assertIn("src/Main.java", result["unsupported_source_files"])

    def test_dependency_graph_reports_oversized_source(self):
        self.write("src/large.py", "x" * (2 * 1024 * 1024 + 1))
        result = build_graph(str(self.root))
        self.assertIn(
            "size_limit",
            result["diagnostics"]["skipped_files"][0]["reason"],
        )

    def test_trace_follows_python_calls_and_depth(self):
        self.write(
            "app.py",
            "def first():\n    second()\n\ndef second():\n    third()\n\ndef third():\n    return 1\n",
        )
        output = trace_flow(self.root, "first", 2)
        self.assertIn("app.py:1", output)
        self.assertIn("app.py:4", output)
        self.assertIn("app.py:7", output)
        self.assertIn("由 `first` 调用", output)

    def test_trace_js_does_not_leak_into_next_function(self):
        self.write(
            "app.ts",
            "function first() { second(); }\n"
            "function second() { return 1; }\n"
            "function unrelated() { third(); }\n"
            "function third() { return 3; }\n",
        )
        output = trace_flow(self.root, "first", 3)
        self.assertIn("second", output)
        self.assertNotIn("third", output)

    def test_trace_python_does_not_include_nested_function_calls(self):
        self.write(
            "app.py",
            "def outer():\n"
            "    def inner():\n"
            "        sensitive()\n"
            "    return inner\n\n"
            "def sensitive():\n"
            "    return 1\n",
        )
        output = trace_flow(self.root, "outer", 2)
        self.assertNotIn("sensitive", output)

    def test_trace_uses_imports_to_disambiguate_same_named_functions(self):
        self.write(
            "main.py",
            "from a import helper\n\ndef start():\n    helper()\n",
        )
        self.write("a.py", "def helper():\n    from_a()\n\ndef from_a():\n    pass\n")
        self.write("b.py", "def helper():\n    from_b()\n\ndef from_b():\n    pass\n")
        output = trace_flow(self.root, "start", 3)
        self.assertIn("from_a", output)
        self.assertNotIn("from_b", output)

    def test_cli_rejects_negative_depth(self):
        process = subprocess.run(
            [sys.executable, str(SCRIPTS / "追踪流程.py"), str(self.root), "x", "--depth", "-1"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(0, process.returncode)
        self.assertIn("非负整数", process.stderr)


if __name__ == "__main__":
    unittest.main()
