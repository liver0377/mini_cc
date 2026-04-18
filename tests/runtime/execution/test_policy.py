from __future__ import annotations

from mini_cc.runtime.execution.policy import ExecutionPolicy


class TestExecutionPolicyToolAllowed:
    def test_default_allows_all(self) -> None:
        policy = ExecutionPolicy()
        allowed, _ = policy.is_tool_allowed("bash")
        assert allowed is True

    def test_readonly_blocks_write_tools(self) -> None:
        policy = ExecutionPolicy(readonly=True)
        allowed, reason = policy.is_tool_allowed("file_edit")
        assert allowed is False
        assert "只读" in reason

    def test_readonly_blocks_file_write(self) -> None:
        policy = ExecutionPolicy(readonly=True)
        allowed, reason = policy.is_tool_allowed("file_write")
        assert allowed is False
        assert "只读" in reason

    def test_readonly_allows_read_tools(self) -> None:
        policy = ExecutionPolicy(readonly=True)
        for tool in ("file_read", "glob", "grep", "scan_dir", "plan_agents"):
            allowed, _ = policy.is_tool_allowed(tool)
            assert allowed is True, f"{tool} should be allowed"

    def test_readonly_blocks_bash(self) -> None:
        policy = ExecutionPolicy(readonly=True)
        allowed, reason = policy.is_tool_allowed("bash")
        assert allowed is False
        assert "只读" in reason

    def test_allowed_tools_whitelist(self) -> None:
        policy = ExecutionPolicy(allowed_tools=frozenset({"file_read", "glob"}))
        allowed, _ = policy.is_tool_allowed("file_read")
        assert allowed is True

        allowed, reason = policy.is_tool_allowed("bash")
        assert allowed is False
        assert "不在允许列表" in reason

    def test_readonly_and_allowed_tools_combined(self) -> None:
        policy = ExecutionPolicy(
            readonly=True,
            allowed_tools=frozenset({"file_read", "file_edit"}),
        )
        allowed, reason = policy.is_tool_allowed("file_edit")
        assert allowed is False
        assert "只读" in reason


class TestExecutionPolicyPathScope:
    def test_default_allows_all_paths(self) -> None:
        policy = ExecutionPolicy()
        allowed, _ = policy.is_path_in_scope("/any/path/file.py")
        assert allowed is True

    def test_dot_scope_allows_all(self) -> None:
        policy = ExecutionPolicy(scope_paths=["."])
        allowed, _ = policy.is_path_in_scope("/any/path/file.py")
        assert allowed is True

    def test_relative_path_within_scope(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, _ = policy.is_path_in_scope("src/main.py")
        assert allowed is True

    def test_relative_path_outside_scope(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, reason = policy.is_path_in_scope("tests/test_main.py")
        assert allowed is False
        assert "scope" in reason

    def test_absolute_path_within_scope(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, _ = policy.is_path_in_scope("/project/src/app.py")
        assert allowed is True

    def test_absolute_path_outside_scope(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, reason = policy.is_path_in_scope("/project/tests/test.py")
        assert allowed is False
        assert "scope" in reason

    def test_multiple_scope_paths(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src", "lib"],
            workspace_root="/project",
        )
        allowed, _ = policy.is_path_in_scope("src/main.py")
        assert allowed is True
        allowed, _ = policy.is_path_in_scope("lib/utils.py")
        assert allowed is True
        allowed, _ = policy.is_path_in_scope("tests/test.py")
        assert allowed is False


class TestExecutionPolicyValidateToolCall:
    def test_write_tool_path_in_scope(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, _ = policy.validate_tool_call("file_edit", {"file_path": "src/main.py"})
        assert allowed is True

    def test_write_tool_path_outside_scope(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, reason = policy.validate_tool_call("file_edit", {"file_path": "tests/test.py"})
        assert allowed is False
        assert "scope" in reason

    def test_read_tool_path_outside_scope_blocked(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, reason = policy.validate_tool_call("file_read", {"file_path": "tests/test.py"})
        assert allowed is False
        assert "scope" in reason

    def test_glob_path_outside_scope_blocked(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, reason = policy.validate_tool_call("glob", {"pattern": "*.py", "path": "tests"})
        assert allowed is False
        assert "scope" in reason

    def test_restricted_scope_blocks_bash(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, reason = policy.validate_tool_call("bash", {"command": "pytest"})
        assert allowed is False
        assert "bash" in reason

    def test_readonly_blocks_write_regardless_of_scope(self) -> None:
        policy = ExecutionPolicy(
            readonly=True,
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, reason = policy.validate_tool_call("file_write", {"file_path": "src/main.py"})
        assert allowed is False
        assert "只读" in reason

    def test_write_tool_no_file_path(self) -> None:
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        allowed, _ = policy.validate_tool_call("file_edit", {"old_string": "a", "new_string": "b"})
        assert allowed is True
