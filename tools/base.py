"""
tools/base.py
Base class for Alex tools.

Each tool declares a name, description, parameter schema and an execute()
method. Tools are exposed to the model through LiveKit's `llm.function_tool`
in `tools/typing/tool.py`; this base class only keeps the implementation
independent from the transport layer.
"""

import logging
from typing import Any

logger = logging.getLogger("alex.tools")


class BaseTool:
    """Subclass this and set name + description + parameters + execute()."""

    name: str = ""
    description: str = ""
    parameters: dict = {"type": "OBJECT", "properties": {}, "required": []}

    def function_declaration(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    async def execute(self, **kwargs: Any) -> str:
        raise NotImplementedError
