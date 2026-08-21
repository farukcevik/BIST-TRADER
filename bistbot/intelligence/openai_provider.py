from __future__ import annotations

from typing import Any

from openai import OpenAI

from bistbot.app.models import LLMAnalysis, LLMCompletion


def _strict_schema() -> dict[str, Any]:
    schema = LLMAnalysis.model_json_schema()

    def normalize(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["additionalProperties"] = False
            node["required"] = list(properties)
        for value in node.values():
            if isinstance(value, dict):
                normalize(value)
            elif isinstance(value, list):
                for item in value:
                    normalize(item)

    normalize(schema)
    return schema


class OpenAILLMProvider:
    """OpenAI Responses API adapter; analysis policy remains in LLMAnalyst."""

    provider_mode = "REAL"

    def __init__(self, *, model: str, api_key: str, timeout_seconds: float = 30, client: Any | None = None):
        if not model.strip():
            raise ValueError("model must not be empty")
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        self.model = model
        self.client = client or OpenAI(api_key=api_key, timeout=timeout_seconds)

    def complete(self, *, system_prompt: str, input_json: str) -> LLMCompletion:
        response = self.client.responses.create(
            model=self.model,
            instructions=system_prompt,
            input=input_json,
            store=False,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "llm_analysis",
                    "strict": True,
                    "schema": _strict_schema(),
                }
            },
        )
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return LLMCompletion(
            content=response.output_text,
            model_name=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
