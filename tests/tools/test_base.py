from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from mini_cc.tools.base import BaseTool, ToolRegistry, ToolResult


class _DummyInput(BaseModel):
    value: str


class _DummyTool(BaseTool):
    @property
    def name(self) -> str:
        return "dummy"

    @property
    def description(self) -> str:
        return "A dummy tool for testing"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _DummyInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = _DummyInput.model_validate(kwargs)
        return ToolResult(output=parsed.value)


class TestToolResult:
    def test_defaults(self) -> None:
        result = ToolResult()
        assert result.output == ""
        assert result.error is None
        assert result.success is True

    def test_error_result(self) -> None:
        result = ToolResult(output="partial", error="something failed", success=False)
        assert result.success is False
        assert result.error == "something failed"


class TestBaseTool:
    def test_to_api_format(self) -> None:
        tool = _DummyTool()
        api = tool.to_api_format()

        assert api["type"] == "function"
        func = api["function"]
        assert func["name"] == "dummy"
        assert func["description"] == "A dummy tool for testing"
        assert "properties" in func["parameters"]
        assert "value" in func["parameters"]["properties"]

    def test_execute(self) -> None:
        tool = _DummyTool()
        result = tool.execute(value="hello")
        assert result.output == "hello"
        assert result.success is True


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        registry = ToolRegistry()
        tool = _DummyTool()

        registry.register(tool)

        assert registry.get("dummy") is tool
        assert registry.get("nonexistent") is None

    def test_all(self) -> None:
        registry = ToolRegistry()
        registry.register(_DummyTool())

        tools = registry.all()
        assert len(tools) == 1
        assert tools[0].name == "dummy"

    def test_to_api_format(self) -> None:
        registry = ToolRegistry()
        registry.register(_DummyTool())

        api = registry.to_api_format()
        assert len(api) == 1
        assert api[0]["type"] == "function"
        assert api[0]["function"]["name"] == "dummy"
        assert "parameters" in api[0]["function"]

    def test_empty_registry(self) -> None:
        registry = ToolRegistry()

        assert registry.all() == []
        assert registry.to_api_format() == []
        assert registry.get("anything") is None


class TestCreateRegistries:
    def test_default_registry_has_all_tools(self) -> None:
        from mini_cc.tools import create_default_registry

        registry = create_default_registry()
        names = {t.name for t in registry.all()}
        assert "file_read" in names
        assert "file_edit" in names
        assert "file_write" in names
        assert "bash" in names
        assert "glob" in names
        assert "grep" in names

    def test_readonly_registry_has_only_read_tools(self) -> None:
        from mini_cc.tools import create_readonly_registry

        registry = create_readonly_registry()
        names = {t.name for t in registry.all()}
        assert "file_read" in names
        assert "glob" in names
        assert "grep" in names
        assert "bash" in names
        assert "file_edit" not in names
        assert "file_write" not in names
