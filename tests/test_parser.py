"""Tests for the Tree-sitter parser module."""

import tempfile
from pathlib import Path

from code_review_graph.parser import CodeParser, _is_test_file

FIXTURES = Path(__file__).parent / "fixtures"


class TestCodeParser:
    def setup_method(self):
        self.parser = CodeParser()

    def test_detect_language_python(self):
        assert self.parser.detect_language(Path("foo.py")) == "python"

    def test_detect_language_typescript(self):
        assert self.parser.detect_language(Path("foo.ts")) == "typescript"

    def test_detect_language_unknown(self):
        assert self.parser.detect_language(Path("foo.txt")) is None

    def test_parse_python_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")

        # Should have File node
        file_nodes = [n for n in nodes if n.kind == "File"]
        assert len(file_nodes) == 1

        # Should find classes
        classes = [n for n in nodes if n.kind == "Class"]
        class_names = {c.name for c in classes}
        assert "BaseService" in class_names
        assert "AuthService" in class_names

        # Should find functions (fixture is in tests/ dir → kind may be Test)
        funcs = [n for n in nodes if n.kind in ("Function", "Test")]
        func_names = {f.name for f in funcs}
        assert "__init__" in func_names
        assert "authenticate" in func_names
        assert "create_auth_service" in func_names
        assert "process_request" in func_names

    def test_parse_python_edges(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")

        edge_kinds = {e.kind for e in edges}
        assert "CONTAINS" in edge_kinds
        assert "IMPORTS_FROM" in edge_kinds
        assert "CALLS" in edge_kinds

        # Should detect inheritance
        inherits = [e for e in edges if e.kind == "INHERITS"]
        assert len(inherits) >= 1
        assert any("AuthService" in e.source and "BaseService" in e.target for e in inherits)

    def test_parse_python_imports(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        assert "os" in import_targets
        assert "pathlib" in import_targets

    def test_parse_python_calls(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        calls = [e for e in edges if e.kind == "CALLS"]
        call_targets = {e.target for e in calls}
        # _resolve_call_targets qualifies same-file definitions
        assert any("_validate_token" in t for t in call_targets)
        assert any("authenticate" in t for t in call_targets)

    def test_parse_typescript_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_typescript.ts")

        classes = [n for n in nodes if n.kind == "Class"]
        class_names = {c.name for c in classes}
        assert "UserRepository" in class_names
        assert "UserService" in class_names

        funcs = [n for n in nodes if n.kind in ("Function", "Test")]
        func_names = {f.name for f in funcs}
        assert "findById" in func_names or "handleGetUser" in func_names

    def test_parse_test_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "test_sample.py")

        # Test functions should be detected
        tests = [n for n in nodes if n.kind == "Test"]
        test_names = {t.name for t in tests}
        assert "test_authenticate_valid" in test_names
        assert "test_process_request_ok" in test_names

    def test_calls_edge_same_file_resolution(self):
        """Call targets defined in the same file should be qualified."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        calls = [e for e in edges if e.kind == "CALLS"]
        file_path = str(FIXTURES / "sample_python.py")

        # create_auth_service() calls AuthService() — a class defined in the same file
        auth_service_calls = [
            e for e in calls if e.target == f"{file_path}::AuthService"
        ]
        assert len(auth_service_calls) >= 1

    def test_calls_edge_cross_file_resolution(self):
        """Call targets imported from another file should resolve to that file's qualified name."""
        _, edges = self.parser.parse_file(FIXTURES / "caller_example.py")
        calls = [e for e in edges if e.kind == "CALLS"]

        sample_path = str((FIXTURES / "sample_python.py").resolve())
        # setup_and_run() calls create_auth_service(), imported from sample_python
        resolved_calls = [
            e for e in calls if e.target == f"{sample_path}::create_auth_service"
        ]
        assert len(resolved_calls) == 1

    def test_same_file_calls_resolved(self):
        """Same-file call targets should be resolved to qualified names."""
        _, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        calls = [e for e in edges if e.kind == "CALLS"]
        # _validate_token is defined in the same file, so it should be qualified
        resolved_calls = [e for e in calls if "_validate_token" in e.target and "::" in e.target]
        assert len(resolved_calls) >= 1

    def test_calls_edge_decorated_function_resolution(self):
        """Decorated functions should be in defined_names and resolvable as call targets."""
        _, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        calls = [e for e in edges if e.kind == "CALLS"]
        file_path = str(FIXTURES / "sample_python.py")

        # guarded_process() calls process_request() — both in the same file,
        # but guarded_process is wrapped in a decorated_definition node
        resolved = [e for e in calls if e.target == f"{file_path}::process_request"
                    and "guarded_process" in e.source]
        assert len(resolved) == 1

    def test_multiple_calls_to_same_function(self):
        """Multiple calls to the same function on different lines should each produce an edge."""
        _, edges = self.parser.parse_file(FIXTURES / "multi_call_example.py")
        calls = [e for e in edges if e.kind == "CALLS" and "_internal_request" in e.target]
        assert len(calls) == 2
        lines = {e.line for e in calls}
        assert len(lines) == 2  # distinct line numbers

    def test_parse_nonexistent_file(self):
        nodes, edges = self.parser.parse_file(Path("/nonexistent/file.py"))
        assert nodes == []
        assert edges == []

    def test_parse_unsupported_extension(self):
        nodes, edges = self.parser.parse_file(Path("readme.txt"))
        assert nodes == []
        assert edges == []

    def test_tested_by_edges_generated(self):
        """Test files should produce TESTED_BY edges when tests call production code."""
        nodes, edges = self.parser.parse_file(FIXTURES / "test_sample.py")
        tested_by = [e for e in edges if e.kind == "TESTED_BY"]
        assert len(tested_by) >= 1

    def test_recursion_depth_guard(self):
        """Parser should not crash on deeply nested code."""
        # Generate Python code with many nested functions (> _MAX_AST_DEPTH)
        depth = 200
        lines = []
        for i in range(depth):
            indent = "    " * i
            lines.append(f"{indent}def func_{i}():")
        lines.append("    " * depth + "pass")
        source = "\n".join(lines).encode("utf-8")

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(source)
            f.flush()
            path = Path(f.name)

        try:
            # Should NOT raise RecursionError
            nodes, edges = self.parser.parse_bytes(path, source)
            # We should get some functions but not all 200 due to depth cap
            funcs = [n for n in nodes if n.kind == "Function"]
            assert len(funcs) > 0
            assert len(funcs) < depth  # capped by _MAX_AST_DEPTH
        finally:
            path.unlink(missing_ok=True)

    def test_module_file_cache_bounded(self):
        """Module file cache should not grow unboundedly."""
        parser = CodeParser()
        # Fill the cache up to the limit
        for i in range(parser._MODULE_CACHE_MAX + 100):
            parser._module_file_cache[f"key_{i}"] = f"/path/to/mod_{i}.py"
        # Trigger a resolve which should clear the cache
        parser._resolve_module_to_file("os", "/test/file.py", "python")
        assert len(parser._module_file_cache) <= parser._MODULE_CACHE_MAX

    # --- Vue SFC tests ---

    def test_detect_language_vue(self):
        assert self.parser.detect_language(Path("App.vue")) == "vue"

    def test_parse_vue_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")

        # Should have File node with language=vue
        file_nodes = [n for n in nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].language == "vue"

        # Should find functions from <script setup> (fixture is in tests/ → kind may be Test)
        funcs = [n for n in nodes if n.kind in ("Function", "Test")]
        func_names = {f.name for f in funcs}
        assert "increment" in func_names
        assert "onSelectUser" in func_names
        assert "fetchUsers" in func_names

    def test_parse_vue_imports(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        assert "vue" in import_targets
        assert "./UserList.vue" in import_targets

    def test_parse_vue_calls(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        calls = [e for e in edges if e.kind == "CALLS"]
        call_targets = {e.target for e in calls}
        assert "log" in call_targets or "console.log" in call_targets or any(
            "log" in t for t in call_targets
        )

    def test_parse_vue_contains_edges(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        contains = [e for e in edges if e.kind == "CONTAINS"]
        assert len(contains) >= 1

    def test_parse_vue_line_numbers_offset(self):
        """Line numbers should be offset to reflect position in the .vue file."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        funcs = [n for n in nodes if n.kind in ("Function", "Test") and n.name == "increment"]
        assert len(funcs) == 1
        # increment() is on line 22 of the .vue file (inside <script setup> starting at line 9)
        assert funcs[0].line_start > 9

    def test_parse_vue_nodes_have_vue_language(self):
        """All extracted nodes from Vue SFC should have language='vue'."""
        nodes, _ = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        for node in nodes:
            assert node.language == "vue"

    def test_parse_vue_empty_script(self):
        """Vue file with no script block should still produce a File node."""
        source = b"<template><div>Hello</div></template>\n"
        path = Path("empty_script.vue")
        nodes, edges = self.parser.parse_bytes(path, source)
        assert len(nodes) == 1
        assert nodes[0].kind == "File"

    def test_parse_vue_js_default(self):
        """Vue file without lang attr should parse script as JavaScript."""
        source = (
            b"<script>\n"
            b"export default {\n"
            b"  methods: {\n"
            b"    greet() { return 'hi' }\n"
            b"  }\n"
            b"}\n"
            b"</script>\n"
        )
        path = Path("js_default.vue")
        nodes, edges = self.parser.parse_bytes(path, source)
        funcs = [n for n in nodes if n.kind == "Function"]
        func_names = {f.name for f in funcs}
        assert "greet" in func_names

    # --- Dart tests ---

    def test_detect_language_dart(self):
        assert self.parser.detect_language(Path("main.dart")) == "dart"

    def test_parse_dart_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")

        file_nodes = [n for n in nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].language == "dart"

        classes = [n for n in nodes if n.kind == "Class"]
        class_names = {c.name for c in classes}
        assert "Animal" in class_names
        assert "Dog" in class_names
        assert "SwimmingMixin" in class_names
        assert "PetType" in class_names

        funcs = [n for n in nodes if n.kind in ("Function", "Test")]
        func_names = {f.name for f in funcs}
        assert "speak" in func_names
        assert "fetch" in func_names
        assert "_run" in func_names
        assert "create" in func_names
        assert "createDog" in func_names
        assert "swim" in func_names

    def test_parse_dart_imports(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        assert "dart:async" in import_targets
        assert "package:flutter/material.dart" in import_targets

    def test_parse_dart_inheritance(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        inherits = [e for e in edges if e.kind == "INHERITS"]
        assert any("Dog" in e.source and "Animal" in e.target for e in inherits)
        assert any("Dog" in e.source and "SwimmingMixin" in e.target for e in inherits)

    def test_parse_dart_contains_edges(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        contains = [e for e in edges if e.kind == "CONTAINS"]
        # File should contain top-level classes and functions
        file_path = str(FIXTURES / "sample.dart")
        file_contains = [e for e in contains if e.source == file_path]
        assert len(file_contains) >= 1
        # Dog class should contain its methods
        dog_contains = [e for e in contains if "Dog" in e.source]
        dog_targets = {e.target for e in dog_contains}
        assert any("speak" in t for t in dog_targets)
        assert any("fetch" in t for t in dog_targets)

    def test_parse_dart_method_parent(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        funcs = [n for n in nodes if n.kind in ("Function", "Test")]
        # Both Animal and Dog define speak(); check Dog's specifically
        dog_speak = next(
            (f for f in funcs if f.name == "speak" and f.parent_name == "Dog"), None,
        )
        assert dog_speak is not None

    def test_parse_dart_top_level_function_no_parent(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        funcs = [n for n in nodes if n.kind in ("Function", "Test")]
        create_dog = next((f for f in funcs if f.name == "createDog"), None)
        assert create_dog is not None
        assert create_dog.parent_name is None

    # --- tsconfig alias resolution ---

    def test_tsconfig_alias_resolution(self):
        """Alias imports should resolve to absolute file paths."""
        nodes, edges = self.parser.parse_file(FIXTURES / "alias_importer.ts")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        resolved_imports = [e for e in imports if e.target.endswith("utils.ts")]
        assert len(resolved_imports) >= 1, (
            f"Expected resolved alias import, got targets: {[e.target for e in imports]}"
        )

    def test_tsconfig_missing_gracefully_handled(self):
        """Files without a tsconfig should still parse without errors."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = os.path.join(tmp_dir, "no_tsconfig_file.ts")
            with open(tmp_path, "w") as f:
                f.write('import { foo } from "@/bar";\nexport const x = 1;\n')
            nodes, edges = self.parser.parse_file(Path(tmp_path))
            imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
            assert any("@/bar" in e.target for e in imports)

    # --- Vitest/Jest test detection ---

    def test_vitest_test_detection(self):
        """Vitest describe/it/test calls should produce Test nodes."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vitest.test.ts")
        tests = [n for n in nodes if n.kind == "Test"]
        test_names = {t.name for t in tests}
        assert any(n.startswith("describe") or n.startswith("describe:") for n in test_names), (
            f"Expected describe Test node, got: {test_names}"
        )
        assert any(n.startswith("it:") or n.startswith("test:") for n in test_names), (
            f"Expected it/test Test node, got: {test_names}"
        )

    def test_vitest_contains_edges(self):
        """describe Test nodes should CONTAIN it/test Test nodes."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vitest.test.ts")
        describe_nodes = [
            n for n in nodes
            if n.kind == "Test"
            and (n.name.startswith("describe") or n.name.startswith("describe:"))
        ]
        assert len(describe_nodes) >= 1
        it_tests = [
            n for n in nodes
            if n.kind == "Test" and (n.name.startswith("it:") or n.name.startswith("test:"))
        ]
        assert len(it_tests) >= 2

        file_path = str(FIXTURES / "sample_vitest.test.ts")
        describe_qualified = {f"{file_path}::{n.name}" for n in describe_nodes}
        contains_sources = {e.source for e in edges if e.kind == "CONTAINS"}
        assert describe_qualified & contains_sources

    def test_vitest_calls_edges(self):
        """Calls inside test blocks should produce CALLS edges."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vitest.test.ts")
        calls = [e for e in edges if e.kind == "CALLS"]
        assert len(calls) >= 1
        test_names = {n.name for n in nodes if n.kind == "Test"}
        file_path = str(FIXTURES / "sample_vitest.test.ts")
        test_qualified = {f"{file_path}::{name}" for name in test_names}
        call_sources = {e.source for e in calls}
        assert call_sources & test_qualified

    def test_vitest_tested_by_edges(self):
        """TESTED_BY edges should be generated from test calls to production code."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vitest.test.ts")
        tested_by = [e for e in edges if e.kind == "TESTED_BY"]
        assert len(tested_by) >= 1, (
            f"Expected TESTED_BY edges, got none. "
            f"All edges: {[(e.kind, e.source, e.target) for e in edges]}"
        )

    def test_non_test_file_describe_not_special(self):
        """describe() in a non-test file should NOT create Test nodes."""
        import tempfile
        code = (
            b'function describe(name, fn) { fn(); }\n'
            b'describe("test", () => { console.log("hello"); });\n'
        )
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False, prefix="regular_") as f:
            f.write(code)
            tmp_path = Path(f.name)
        try:
            nodes, edges = self.parser.parse_file(tmp_path)
            tests = [n for n in nodes if n.kind == "Test"]
            assert len(tests) == 0, (
                f"Non-test file should not have Test nodes, got: {[t.name for t in tests]}"
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_relative_import_all_names_extracted(self):
        """from .. import a, b should create IMPORTS_FROM edges for BOTH a and b.

        Bug C-01: the old code used `break` after the first dotted_name in
        import_from_statement, so only `a` was extracted and `b` was silently
        dropped.  This test verifies that all names in a relative star-import
        are resolved to files when they exist as sibling modules.
        """
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create a minimal package layout:
            #   pkg/__init__.py
            #   pkg/module_a.py          <- importer
            #   pkg/module_b.py          <- first import target
            #   pkg/module_c.py          <- second import target (was dropped before fix)
            pkg = tmppath / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_bytes(b"")
            # module_b and module_c are the imported modules
            (pkg / "module_b.py").write_bytes(b"def func1(): pass\ndef func2(): pass\n")
            (pkg / "module_c.py").write_bytes(b"def func3(): pass\n")
            # module_a imports both via a relative import
            (pkg / "module_a.py").write_bytes(
                b"from . import module_b, module_c\n"
            )

            nodes, edges = self.parser.parse_file(pkg / "module_a.py")
            import_targets = {e.target for e in edges if e.kind == "IMPORTS_FROM"}

            # Both module_b.py and module_c.py must appear as edge targets.
            module_b_path = str((pkg / "module_b.py").resolve())
            module_c_path = str((pkg / "module_c.py").resolve())
            assert any(module_b_path.replace("\\", "/") in t.replace("\\", "/") for t in import_targets), (
                f"Expected IMPORTS_FROM edge to module_b.py, got: {import_targets}"
            )
            assert any(module_c_path.replace("\\", "/") in t.replace("\\", "/") for t in import_targets), (
                f"Expected IMPORTS_FROM edge to module_c.py, got: {import_targets}"
            )

    def test_is_test_file_inherits_to_all_nodes(self, tmp_path):
        """All nodes in a test file should have is_test=True, regardless of name."""
        src = "class TestBase:\n    def create_task(self):\n        pass\n"

        # In a test file path — ALL nodes should be is_test=True
        test_file = tmp_path / "tests" / "test_helpers.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text(src)

        nodes, _ = self.parser.parse_file(test_file)
        non_file_nodes = [n for n in nodes if n.kind != "File"]
        assert non_file_nodes, "Expected at least one non-File node"
        for node in non_file_nodes:
            assert node.is_test, (
                f"Node {node.name!r} ({node.kind}) in test file should have is_test=True"
            )

    def test_non_test_file_helper_not_flagged(self):
        """Helper methods in non-test files should have is_test=False."""
        src = "class TestBase:\n    def create_task(self):\n        pass\n"

        # Use tempfile so the directory name doesn't contain 'test_' (pytest
        # tmp_path names the dir after the test function, which starts with 'test_'
        # and trips the greedy regex test_.*\.py$).
        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = Path(tmpdir) / "production" / "helpers.py"
            src_file.parent.mkdir(parents=True)
            src_file.write_text(src)

            nodes, _ = self.parser.parse_file(src_file)
            helper = next((n for n in nodes if n.name == "create_task"), None)
            assert helper is not None, "Expected to find 'create_task' node"
            assert not helper.is_test, (
                f"Node 'create_task' in non-test file should have is_test=False"
            )


class TestIsTestFilePatterns:
    """Unit tests for _is_test_file directory-hierarchy detection."""

    # ---- paths that must match ----
    def test_tests_dir(self):
        assert _is_test_file("tests/test_parser.py")

    def test_test_dir(self):
        assert _is_test_file("test/unit/auth.py")

    def test_nested_tests_dir(self):
        assert _is_test_file("src/tests/utils.py")

    def test_jest_tests_dir(self):
        assert _is_test_file("src/__tests__/auth.py")

    def test_jest_tests_dir_root(self):
        assert _is_test_file("__tests__/auth.js")

    def test_spec_dir(self):
        assert _is_test_file("spec/auth_spec.rb")

    def test_nested_spec_dir(self):
        assert _is_test_file("src/spec/helpers.rb")

    def test_testing_dir(self):
        assert _is_test_file("testing/helpers.py")

    def test_nested_testing_dir(self):
        assert _is_test_file("src/testing/mock.py")

    def test_test_prefix_filename(self):
        assert _is_test_file("test_helpers.py")

    def test_test_suffix_filename(self):
        assert _is_test_file("auth_test.py")

    # ---- paths that must NOT match ----
    def test_plain_source_file(self):
        assert not _is_test_file("auth.py")

    def test_nontests_dir_not_matched(self):
        assert not _is_test_file("nontests/utils.py")

    def test_contesting_dir_not_matched(self):
        assert not _is_test_file("contesting/utils.py")

    def test_protest_dir_not_matched(self):
        assert not _is_test_file("protest/utils.py")

    def test_windows_backslash_tests_dir(self):
        assert _is_test_file("tests\\fixtures\\sample.py")

    def test_windows_backslash_jest_dir(self):
        assert _is_test_file("src\\__tests__\\auth.py")

    def test_windows_backslash_non_test(self):
        assert not _is_test_file("src\\auth.py")
