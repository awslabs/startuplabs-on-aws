from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.platform.ports.compute import (
    ContainerRuntime,
    InvocationResult,
    MachineFleet,
)
from app.platform.ports.compute import FunctionRuntime as _Functions
from app.shared import errors


class ComputeFacade:
    """Run work by logical name. `functions` maps logical name -> physical name."""

    def __init__(
        self,
        functions: _Functions,
        function_names: Mapping[str, str],
        containers: ContainerRuntime | None = None,
        machines: MachineFleet | None = None,
    ) -> None:
        self._functions = functions
        self._names = dict(function_names)
        self.containers = containers
        self.machines = machines

    def run_function(self, logical_name: str, payload: Any) -> InvocationResult:
        if logical_name not in self._names:
            raise errors.NotFound(f"function {logical_name}")
        result = self._functions.invoke(self._names[logical_name], payload)
        if result.function_error:
            raise errors.PlatformUnavailable(f"function {logical_name} failed")
        return result

    def function_names(self) -> list[str]:
        return sorted(self._names)
