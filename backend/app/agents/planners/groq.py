"""Groq planner: asks a Groq-hosted model for a DraftPlan using strict structured outputs.

Uses the official ``groq`` SDK: ``client.chat.completions.create`` with
``response_format={"type": "json_schema", "json_schema": {"name", "strict": True, "schema"}}``.
Strict mode (constrained decoding) requires every property to be ``required`` and
``additionalProperties: false``, so the schema sent is derived from the SAME Pydantic model
(``strict_json_schema``); the reply is then re-validated with that Pydantic model and goes
through the same plan validator / tool allow-list as every other planner. The SDK's own
retries are disabled (``max_retries=0``): the planning graph owns retry and fallback policy.

Model: GROQ_MODEL must support strict structured outputs. Verified 2026-11-05 against
console.groq.com/docs: ``openai/gpt-oss-120b`` and ``openai/gpt-oss-20b`` are production
models with strict json_schema support (llama-3.3-70b-versatile was shut down 2026-08-16).

Langfuse: one "groq.chat.completions.create" generation per call with model, token usage,
finish reason and response id. Prompt and instruction text are never sent.
"""

import copy
import logging
from collections.abc import Collection
from typing import Any

import groq
from pydantic import BaseModel

from app.agents.planners.base import PROVIDER_UNAVAILABLE_HTTP_STATUSES, PlannerError
from app.agents.planners.claude import build_system_prompt
from app.observability import get_telemetry
from app.schemas.plan import DraftPlan

logger = logging.getLogger(__name__)

_DROP_KEYS = ("default", "title", "examples")


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema made acceptable to strict structured outputs: ``$defs`` inlined,
    every object property required and ``additionalProperties: false``, defaults/titles dropped.
    Pydantic re-validation of the reply is unchanged, so this only tightens the output."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return resolve(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
        # "properties" maps field NAMES to schemas: keep every name (a field may be called "title").
        out = {k: ({name: resolve(sub) for name, sub in v.items()} if k == "properties" else resolve(v))
               for k, v in node.items() if k not in _DROP_KEYS}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return resolve(schema)


def _error_code(exc: groq.APIStatusError) -> str | None:
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body) if isinstance(body.get("error", body), dict) else {}
    code = error.get("code")
    return code if isinstance(code, str) and code.isidentifier() and len(code) <= 40 else None


def translate_api_error(exc: groq.APIStatusError) -> PlannerError:
    """Map a Groq SDK status error to a PlannerError: the HTTP status only, never the
    provider's message, the response body or any credential."""
    status = exc.status_code
    label = f"HTTP {status}"
    if status == 429:
        return PlannerError("PLANNER_RATE_LIMITED", f"Groq API rate limit or quota hit ({label}).", retryable=True,
                            provider_unavailable=True, http_status=status)
    if status in (401, 403):
        return PlannerError("PLANNER_AUTH_ERROR", f"Groq API rejected the credentials ({label}). Check GROQ_API_KEY.",
                            http_status=status)
    if status == 400 and _error_code(exc) == "json_validate_failed":
        # The model's output did not match the schema: a schema failure, not an outage.
        return PlannerError("INVALID_LLM_OUTPUT", "Groq returned output that does not match the plan schema.",
                            retryable=True, http_status=status)
    if status in (400, 404, 413, 422):
        return PlannerError("PLANNER_REQUEST_REJECTED", f"Groq API rejected the request ({label}). Check GROQ_MODEL.",
                            http_status=status)
    if status == 408 or status >= 500:
        return PlannerError("PLANNER_API_ERROR", f"Groq API error ({label}).", retryable=True,
                            provider_unavailable=status in PROVIDER_UNAVAILABLE_HTTP_STATUSES, http_status=status)
    return PlannerError("PLANNER_API_ERROR", f"Groq API error ({label}).", http_status=status)


def groq_usage_details(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    out: dict[str, int] = {}
    for key, attr in (("input", "prompt_tokens"), ("output", "completion_tokens"), ("total", "total_tokens")):
        value = getattr(usage, attr, None)
        if isinstance(value, int):
            out[key] = value
    return out


class GroqPlanner:
    name = "groq"

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        max_tokens: int,
        available_documents: Collection[str] | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._available_documents = frozenset(available_documents) if available_documents else None

    @property
    def model(self) -> str:
        return self._model

    def plan(self, instruction: str, *, max_steps: int, allowed_integrations=None, drive_folders=None,
             notion_parents=None, teams_channels=None) -> DraftPlan:
        system_prompt = build_system_prompt(max_steps, self._available_documents, allowed_integrations, drive_folders,
                                            notion_parents, teams_channels)
        with self._generation(instruction, system_prompt) as generation:
            draft = self._call(instruction, system_prompt, generation)
            generation.update(output={"step_count": len(draft.steps)})
            return draft

    def _generation(self, instruction: str, system_prompt: str, **extra_input: Any):
        # Lengths only: the prompt and the instruction are NOT sent to Langfuse.
        return get_telemetry().observe(
            "groq.chat.completions.create",
            as_type="generation",
            model=self._model,
            model_parameters={"max_completion_tokens": self._max_tokens, "temperature": 0},
            input={"instruction_chars": len(instruction), "system_prompt_chars": len(system_prompt), **extra_input},
        )

    def _call(self, instruction: str, system_prompt: str, generation, output_format: type[BaseModel] = DraftPlan):
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"<instruction>\n{instruction}\n</instruction>"},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": output_format.__name__, "strict": True,
                                    "schema": strict_json_schema(output_format)},
                },
                max_completion_tokens=self._max_tokens,
                temperature=0,
            )
        # Most specific first: APITimeoutError subclasses APIConnectionError.
        except groq.APITimeoutError as exc:
            raise PlannerError("PLANNER_TIMEOUT", "Groq request timed out.", retryable=True,
                               provider_unavailable=True) from exc
        except groq.APIConnectionError as exc:
            raise PlannerError("PLANNER_CONNECTION_ERROR", "Could not connect to the Groq API.", retryable=True,
                               provider_unavailable=True) from exc
        except groq.APIStatusError as exc:
            error = translate_api_error(exc)
            logger.warning("groq planner error HTTP %s code=%s retryable=%s", exc.status_code, error.code,
                           error.retryable)
            generation.update(metadata={"http_status": exc.status_code})
            raise error from exc

        choice = response.choices[0] if getattr(response, "choices", None) else None
        finish = getattr(choice, "finish_reason", None)
        generation.update(
            usage_details=groq_usage_details(response),
            metadata={"finish_reason": finish, "response_id": getattr(response, "id", None),
                      "model_version": getattr(response, "model", None)},
        )
        logger.info("groq planner response response_id=%s finish_reason=%s", getattr(response, "id", None), finish)

        message = getattr(choice, "message", None)
        if finish == "content_filter" or getattr(message, "refusal", None):
            raise PlannerError("PLANNER_REFUSED", "Groq declined to plan this instruction.")
        if finish == "length":
            raise PlannerError("INVALID_LLM_OUTPUT", "Groq output was truncated (GROQ_MAX_TOKENS reached).",
                               retryable=True)
        text = getattr(message, "content", None)
        if not text:
            raise PlannerError("INVALID_LLM_OUTPUT", "Groq returned no structured plan.", retryable=True)
        try:
            return output_format.model_validate_json(text)
        except ValueError as exc:  # pydantic.ValidationError / JSON decode errors
            raise PlannerError("INVALID_LLM_OUTPUT", "Groq returned output that does not match the plan schema.",
                               retryable=True) from exc

