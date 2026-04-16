from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from mini_cc.tools.base import BaseTool, ToolResult


class BashInput(BaseModel):
    command: str
    timeout: int = 120000


class Bash(BaseTool):
    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "执行 shell 命令"

    @property
    def input_schema(self) -> type[BaseModel]:
        return BashInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = BashInput.model_validate(kwargs)

        try:
            result = subprocess.run(
                parsed.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=parsed.timeout / 1000,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(error=f"命令超时 ({parsed.timeout}ms)", success=False)
        except OSError as e:
            return ToolResult(error=f"执行失败: {e}", success=False)

        output = result.stdout
        if result.stderr:
            output = f"{output}\nstderr:\n{result.stderr}" if output else f"stderr:\n{result.stderr}"

        if result.returncode != 0:
            return ToolResult(
                output=output.strip(),
                error=f"命令退出码: {result.returncode}",
                success=False,
            )

        return ToolResult(output=output.strip())

    async def async_execute(self, **kwargs: Any) -> ToolResult:
        interrupt_check = kwargs.pop("_is_interrupted", None)
        parsed = BashInput.model_validate(kwargs)
        checker = interrupt_check if callable(interrupt_check) else (lambda: False)
        return await self._run_async(parsed.command, parsed.timeout, checker)

    async def _run_async(
        self,
        command: str,
        timeout: int,
        is_interrupted: Callable[[], bool],
    ) -> ToolResult:
        started = time.monotonic()
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            while True:
                if is_interrupted():
                    await self._terminate_process(process)
                    return ToolResult(error="命令已取消", success=False)
                if process.returncode is not None:
                    break
                if (time.monotonic() - started) * 1000 >= timeout:
                    await self._terminate_process(process)
                    return ToolResult(error=f"命令超时 ({timeout}ms)", success=False)
                await asyncio.sleep(0.05)

            stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise

        output = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if stderr:
            output = f"{output}\nstderr:\n{stderr}" if output else f"stderr:\n{stderr}"

        if process.returncode != 0:
            return ToolResult(
                output=output.strip(),
                error=f"命令退出码: {process.returncode}",
                success=False,
            )
        return ToolResult(output=output.strip())

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.2)
        except TimeoutError:
            process.kill()
            await process.wait()
        await process.communicate()
