from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, TypeVar


T = TypeVar("T")
FailureCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class StructuredModelFailure:
    task: str
    attempt: int
    max_attempts: int
    error: str
    finish_reason: str | None
    max_tokens: int
    raw_response: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "error": self.error,
            "finish_reason": self.finish_reason,
            "max_tokens": self.max_tokens,
            "raw_response": self.raw_response[:8000],
        }


class StructuredModelError(RuntimeError):
    def __init__(self, task: str, failures: list[StructuredModelFailure]) -> None:
        self.task = task
        self.failures = failures
        latest = failures[-1] if failures else None
        detail = latest.error if latest else "unknown model response failure"
        finish = (
            f"; finish_reason={latest.finish_reason}"
            if latest and latest.finish_reason
            else ""
        )
        attempt_word = "attempt" if len(failures) == 1 else "attempts"
        super().__init__(
            f"{task} failed after {len(failures)} {attempt_word}: {detail}{finish}"
        )


def call_structured_completion(
    *,
    client,
    model: str,
    messages: list[dict[str, Any]],
    response_format: dict[str, Any],
    parser: Callable[[dict[str, Any]], T],
    task: str,
    max_attempts: int = 3,
    max_tokens: int = 4000,
    retry_max_tokens: int | None = None,
    temperature: float = 0.1,
    on_failure: FailureCallback | None = None,
) -> T:
    """Call an OpenAI-compatible model until valid structured output is returned."""

    attempts = max(1, max_attempts)
    expanded_tokens = max(max_tokens, retry_max_tokens or max_tokens)
    failures: list[StructuredModelFailure] = []
    correction: str | None = None
    token_budget = max_tokens

    for attempt in range(1, attempts + 1):
        attempt_messages = list(messages)
        if correction:
            attempt_messages.append(
                {
                    "role": "user",
                    "content": (
                        "The previous response did not satisfy the required JSON "
                        f"contract: {correction}. Return only a complete object that "
                        "matches the supplied schema. Keep reason fields concise."
                    ),
                }
            )
        raw = ""
        finish_reason: str | None = None
        try:
            response = client.chat.completions.create(
                model=model,
                messages=attempt_messages,
                max_tokens=token_budget,
                temperature=temperature if attempt == 1 else 0.0,
                response_format=response_format,
            )
            choice = response.choices[0]
            finish_value = getattr(choice, "finish_reason", None)
            finish_reason = str(finish_value) if finish_value is not None else None
            raw = _response_text(response)
            if not raw:
                raise ValueError("model returned an empty response")
            data = _extract_json_object(raw)
            return parser(data)
        except Exception as exc:
            failure = StructuredModelFailure(
                task=task,
                attempt=attempt,
                max_attempts=attempts,
                error=str(exc),
                finish_reason=finish_reason,
                max_tokens=token_budget,
                raw_response=raw,
            )
            failures.append(failure)
            if on_failure:
                try:
                    on_failure(failure.to_dict())
                except Exception:
                    pass
            if finish_reason == "length":
                token_budget = expanded_tokens
            correction = str(exc)[:500]

    raise StructuredModelError(task, failures)


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Structured response must be a JSON object")
    return value


def _response_text(response) -> str:
    choice = response.choices[0]
    message = choice.message
    for key in ("content", "reasoning_content", "reasoning", "text"):
        value = getattr(message, key, None)
        text = _stringify_content(value).strip()
        if text:
            return text
    return ""


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)
