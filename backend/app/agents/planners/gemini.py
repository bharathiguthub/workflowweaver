"""Gemini planner: asks Google Gemini for a DraftPlan using structured JSON output.

Uses the official Google Gen AI SDK (``google-genai``): ``client.models.generate_content``
with ``response_mime_type="application/json"`` and ``response_schema=<Pydantic model>``.
The reply is re-validated against the same Pydantic model, so the plan schema, the
validator, the LangGraph retry edge and the approval flow are identical to the Claude
planner. The SDK's own retries are disabled at client construction (attempts=1): the graph
owns retry policy. Every SDK failure becomes a ``PlannerError`` with the same codes the
Claude planner uses.

Langfuse: one "gemini.models.generate_content" generation per call with model, token usage
(input / output incl. thinking / cached / total), finish reason and response id. Latency
is the observation's duration. Gemini does not return a cost; Langfuse derives it from its
model price table when it knows the model. Prompt and instruction text are never sent.
"""

import json
import logging
import re
from collections.abc import Collection
from typing import Any

from google.genai import errors, types
from pydantic import BaseModel

from app.agents.planners.base import PROVIDER_UNAVAILABLE_HTTP_STATUSES, PlannerError
from app.agents.planners.claude import build_system_prompt
from app.observability import get_telemetry
from app.schemas.plan import DraftPlan

logger = logging.getLogger(__name__)

try:  # the SDK may use httpx or its httpx2 fork; catch transport errors from both
    import httpx

    _TIMEOUTS: tuple[type[BaseException], ...] = (httpx.TimeoutException,)
    _TRANSPORT: tuple[type[BaseException], ...] = (httpx.TransportError,)
except ImportError:  # pragma: no cover
    _TIMEOUTS, _TRANSPORT = (), ()
try:
    import httpx2

    _TIMEOUTS += (httpx2.TimeoutException,)
    _TRANSPORT += (httpx2.TransportError,)
except ImportError:  # pragma: no cover
    pass

_REFUSAL_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}


def _api_key_invalid(exc: errors.APIError) -> bool:
    """Gemini answers an invalid key with HTTP 400 INVALID_ARGUMENT, reason API_KEY_INVALID."""
    try:
        return "API_KEY_INVALID" in json.dumps(exc.details)
    except (TypeError, ValueError):
        return False


_STATUS_NAME = re.compile(r"[A-Z][A-Z_]{1,39}")


def safe_status_name(exc: errors.APIError) -> str | None:
    """Google's canonical status (e.g. UNAVAILABLE, RESOURCE_EXHAUSTED) - a fixed enum, so it
    is safe to report. Anything else (free text, odd casing, long values) is dropped."""
    status = exc.status if isinstance(exc.status, str) else None
    return status if status and _STATUS_NAME.fullmatch(status) else None


def http_label(exc: errors.APIError) -> str:
    """"HTTP 503 UNAVAILABLE" - the status code plus the safe status name, if any."""
    name = safe_status_name(exc)
    return f"HTTP {exc.code or 0}" + (f" {name}" if name else "")


def translate_api_error(exc: errors.APIError) -> PlannerError:
    """Map an SDK APIError to a PlannerError. Reports the HTTP status code and Google's status
    NAME only; never the response body, the provider's message, or any credential."""
    code = exc.code or 0
    label = http_label(exc)
    unavailable = code in PROVIDER_UNAVAILABLE_HTTP_STATUSES  # 429 / 500 / 502 / 503 (/ 408 / 504): may fall back
    if code == 429:
        return PlannerError("PLANNER_RATE_LIMITED", f"Gemini API rate limit or quota hit ({label}).", retryable=True,
                            provider_unavailable=True, http_status=code)
    if code in (401, 403) or (code == 400 and _api_key_invalid(exc)):
        return PlannerError("PLANNER_AUTH_ERROR", f"Gemini API rejected the credentials ({label}). Check GEMINI_API_KEY.",
                            http_status=code)
    if code in (400, 404):
        return PlannerError("PLANNER_REQUEST_REJECTED", f"Gemini API rejected the request ({label}). Check GEMINI_MODEL.",
                            http_status=code)
    if code == 408 or code >= 500:
        return PlannerError("PLANNER_API_ERROR", f"Gemini API error ({label}).", retryable=True,
                            provider_unavailable=unavailable, http_status=code)
    return PlannerError("PLANNER_API_ERROR", f"Gemini API error ({label}).", http_status=code)


def gemini_usage_details(response: Any) -> dict[str, int]:
    """Token usage as Langfuse usage_details. ``output`` includes thinking tokens, which
    Gemini bills as output. Only fields the API actually returned."""
    usage = getattr(response, "usage_metadata", None)

    def count(attr: str) -> int | None:
        value = getattr(usage, attr, None)
        return value if isinstance(value, int) else None

    out: dict[str, int] = {}
    prompt, candidates, thoughts = count("prompt_token_count"), count("candidates_token_count"), count(
        "thoughts_token_count")
    if prompt is not None:
        out["input"] = prompt
    if candidates is not None or thoughts is not None:
        out["output"] = (candidates or 0) + (thoughts or 0)
    if thoughts is not None:
        out["output_thinking_tokens"] = thoughts
    if (cached := count("cached_content_token_count")) is not None:
        out["input_cached_tokens"] = cached
    if (total := count("total_token_count")) is not None:
        out["total"] = total
    return out


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    return getattr(reason, "value", reason) if reason is not None else None


class GeminiPlanner:
    name = "gemini"

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        max_output_tokens: int,
        available_documents: Collection[str] | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens
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
            "gemini.models.generate_content",
            as_type="generation",
            model=self._model,
            model_parameters={"max_output_tokens": self._max_output_tokens},
            input={"instruction_chars": len(instruction), "system_prompt_chars": len(system_prompt), **extra_input},
        )

    def _call(self, instruction: str, system_prompt: str, generation, output_format: type[BaseModel] = DraftPlan):
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=output_format,
            max_output_tokens=self._max_output_tokens,
        )
        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=f"<instruction>\n{instruction}\n</instruction>",
                config=config,
            )
        except errors.APIError as exc:
            error = translate_api_error(exc)
            logger.warning("gemini planner error %s code=%s retryable=%s", http_label(exc), error.code, error.retryable)
            generation.update(metadata={"http_status": exc.code, "status_name": safe_status_name(exc)})
            raise error from exc
        except _TIMEOUTS as exc:
            raise PlannerError("PLANNER_TIMEOUT", "Gemini request timed out.", retryable=True,
                               provider_unavailable=True) from exc
        except _TRANSPORT as exc:
            raise PlannerError("PLANNER_CONNECTION_ERROR", "Could not connect to the Gemini API.",
                               retryable=True, provider_unavailable=True) from exc

        finish = _finish_reason(response)
        generation.update(
            usage_details=gemini_usage_details(response),
            metadata={"finish_reason": finish, "response_id": getattr(response, "response_id", None),
                      "model_version": getattr(response, "model_version", None)},
        )
        logger.info("gemini planner response response_id=%s finish_reason=%s",
                    getattr(response, "response_id", None), finish)

        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise PlannerError("PLANNER_REFUSED", "Gemini blocked this instruction.")
        if finish in _REFUSAL_REASONS:
            raise PlannerError("PLANNER_REFUSED", f"Gemini declined to plan this instruction ({finish}).")
        if finish == "MAX_TOKENS":
            raise PlannerError("INVALID_LLM_OUTPUT",
                               "Gemini output was truncated (GEMINI_MAX_OUTPUT_TOKENS reached).", retryable=True)

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, output_format):
            return parsed
        try:
            text = response.text
        except ValueError:  # the SDK raises when there is no text part
            text = None
        if not text:
            raise PlannerError("INVALID_LLM_OUTPUT", "Gemini returned no structured plan.", retryable=True)
        try:
            return output_format.model_validate_json(text)
        except ValueError as exc:  # pydantic.ValidationError / JSON decode errors
            raise PlannerError("INVALID_LLM_OUTPUT", "Gemini returned output that does not match the plan schema.",
                               retryable=True) from exc
