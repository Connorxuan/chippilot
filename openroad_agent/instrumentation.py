"""Optional tool filtering and instrumentation shared by adapter integrations."""

from __future__ import annotations

from typing import Any, Iterable

from langchain_core.tools import BaseTool
from pydantic import PrivateAttr


class InstrumentedTool(BaseTool):
    """Transparent BaseTool proxy with before/after execution callbacks."""

    _delegate: BaseTool = PrivateAttr()
    _observer: Any = PrivateAttr()

    def __init__(self, delegate: BaseTool, observer: Any):
        super().__init__(
            name=delegate.name,
            description=delegate.description,
            args_schema=delegate.args_schema,
            return_direct=delegate.return_direct,
            response_format=getattr(delegate, "response_format", "content"),
        )
        self._delegate = delegate
        self._observer = observer

    def _run(self, *args: Any, run_manager: Any = None, **kwargs: Any) -> Any:
        arguments = kwargs if kwargs else list(args)
        started = self._observer.before_tool(self.name, arguments)
        try:
            result = self._delegate.invoke(kwargs if kwargs else args[0] if len(args) == 1 else args)
        except BaseException as exc:
            self._observer.after_tool(self.name, started, error=f"{type(exc).__name__}: {exc}")
            raise
        self._observer.after_tool(self.name, started, result=result)
        return result

    async def _arun(self, *args: Any, run_manager: Any = None, **kwargs: Any) -> Any:
        arguments = kwargs if kwargs else list(args)
        started = self._observer.before_tool(self.name, arguments)
        try:
            result = await self._delegate.ainvoke(kwargs if kwargs else args[0] if len(args) == 1 else args)
        except BaseException as exc:
            self._observer.after_tool(self.name, started, error=f"{type(exc).__name__}: {exc}")
            raise
        self._observer.after_tool(self.name, started, result=result)
        return result


def select_tools(
    tools: Iterable[BaseTool],
    tool_catalog: set[str] | None = None,
    observer: Any | None = None,
) -> list[BaseTool]:
    selected = [tool for tool in tools if tool_catalog is None or tool.name in tool_catalog]
    if observer is not None:
        return [InstrumentedTool(tool, observer) for tool in selected]
    return selected
