"""
title: Ask Clarifying Questions
description: Ask the user an open-ended or multiple-choice clarifying question, using the inline chat panel when available.
author: mdelponte
version: 1.3.0
license: MIT
required_open_webui_version: 0.11.0
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

# Open WebUI 0.12.0+ limits inline questions to 60-240 seconds on the server
# and uses 120 seconds when timeout_ms is not an integer, so ask for the
# longest wait it allows.
_INLINE_TIMEOUT_MS = 240_000
_TIMEOUT_MESSAGE = (
    "Clarification timed out before the user answered; do not assume a choice."
)


class _ClarificationError(Exception):
    """A safe, user-facing interaction error."""


class _ClarificationTimeout(_ClarificationError):
    """The user did not answer before Open WebUI's interaction deadline."""


class Tools:
    class Valves(BaseModel):
        UI_MODE: Literal["auto", "inline", "modal"] = Field(
            default="auto",
            description=(
                "auto uses the inline question panel on Open WebUI 0.11.1+ and "
                "the floating dialog on older/unknown versions. Override for "
                "custom builds; inline requires frontend support. On 0.12+, "
                "inline questions expire after 4 minutes; modal has no limit."
            ),
        )

    class UserValves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _server_version() -> tuple[int, ...] | None:
        try:
            from open_webui.env import VERSION
        except (ImportError, AttributeError):
            return None
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(VERSION))
        return tuple(map(int, match.groups())) if match else None

    def _use_inline(self) -> bool:
        if self.valves.UI_MODE != "auto":
            return self.valves.UI_MODE == "inline"
        version = self._server_version()
        return version is not None and version >= (0, 11, 1)

    @classmethod
    def _inline_timeout_ms(cls) -> int | None:
        # 0.11.x waits without a limit when timeout_ms is null. Newer or
        # unknown servers get the longest wait 0.12.0+ allows.
        version = cls._server_version()
        if version is not None and version < (0, 12, 0):
            return None
        return _INLINE_TIMEOUT_MS

    @staticmethod
    async def _emit_status(emitter, description: str, done: bool) -> None:
        if emitter:
            try:
                await emitter(
                    {
                        "type": "status",
                        "data": {"description": description, "done": done},
                    }
                )
            except Exception:  # noqa: BLE001, S110 — best-effort event delivery
                # A disconnected status channel must not discard an answer.
                pass

    @staticmethod
    def _normalize_choices(choices) -> list[str]:
        # Be forgiving of blank/duplicate entries and a lone string at runtime.
        if isinstance(choices, str):
            choices = [choices]
        if not isinstance(choices, (list, tuple)):
            return []
        result = []
        for choice in choices:
            if isinstance(choice, str) and choice.strip():
                choice = choice.strip()
                if choice not in result:
                    result.append(choice)
        return result

    @staticmethod
    def _parse_answer(response, choices: list[str], inline: bool) -> str:
        if isinstance(response, dict) and response.get("error"):
            # Do not expose arbitrary backend exceptions or authenticated URLs.
            if "timed out" in str(response["error"]).lower():
                raise _ClarificationTimeout(_TIMEOUT_MESSAGE)
            raise _ClarificationError(
                "Unable to get clarification. The client disconnected or the request "
                "timed out; check the active browser tab and connection."
            )
        if (
            response is None
            or response is False
            or (isinstance(response, dict) and response.get("status") == "cancelled")
        ):
            raise _ClarificationError(
                "Clarification cancelled. No answer was provided; do not assume a choice."
            )

        if inline:
            if not isinstance(response, dict) or response.get("status") != "answered":
                raise _ClarificationError(
                    "Invalid clarification response. No answer was received."
                )
            answers = response.get("answers")
            answer = answers.get("clarification") if isinstance(answers, dict) else None
            if not isinstance(answer, dict):
                raise _ClarificationError(
                    "Invalid clarification response. No answer was received."
                )
            if answer.get("type") == "option":
                index = answer.get("option_index")
                if type(index) is not int or not 0 <= index < len(choices):
                    raise _ClarificationError(
                        "Invalid clarification choice. No answer was received."
                    )
                # Return the complete original label, not an index or browser echo.
                return choices[index]
            response = answer.get("text") if answer.get("type") == "other" else None
        elif isinstance(response, dict):
            # Older clients can wrap the modal input in {"value": ...}.
            response = response.get("value")

        if not isinstance(response, str) or not response.strip():
            raise _ClarificationError(
                "No clarification answer was provided; do not assume a choice."
            )
        return response.strip()

    async def ask_clarifying_question(
        self,
        question: str,
        choices: list[str] | None = None,
        __event_call__: Callable[[dict], Awaitable[Any]] | None = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Ask one clarifying question and wait for the user's answer before continuing.
        Use when missing information or a meaningful user decision blocks progress.
        Omit choices for an open-ended question, or provide plain answer strings,
        e.g. ["Python", "JavaScript", "Go"]. Custom text is always allowed.
        No IDs, headers, descriptions, or fixed number of choices are required.
        The inline UI marks the first choice as recommended; put your preferred
        option first. Ask questions sequentially, not in parallel.

        :param question: The clarifying question to ask the user.
        :param choices: Optional answer choices as a list of strings. Omit for free text.
        :return: The full selected answer or custom text, or an explicit error if unanswered.
        """
        if not isinstance(question, str) or not question.strip():
            return "Error: Provide a non-empty clarifying question."
        if not __event_call__:
            return "Error: Unable to prompt the user without an active WebUI browser session."

        question = question.strip()
        choices = self._normalize_choices(choices)
        done_description = "Clarification request ended without an answer."
        try:
            await self._emit_status(
                __event_emitter__, "Waiting for your response...", False
            )
            inline = self._use_inline()
            if inline:
                event = {
                    "type": "request:user_input",
                    "data": {
                        "questions": [
                            {
                                "id": "clarification",
                                "header": "Clarification Needed",
                                "question": question,
                                "options": [
                                    {"label": choice, "description": ""}
                                    for choice in choices
                                ],
                                "allow_other": True,
                            }
                        ],
                        "allow_other": True,
                        "timeout_ms": self._inline_timeout_ms(),
                    },
                }
            else:
                event = {
                    "type": "input",
                    "data": {
                        "title": "Clarification Needed",
                        "message": question,
                        "placeholder": "Type your answer here...",
                    },
                }
                if choices:
                    event["data"].update(
                        {
                            "placeholder": "Select an answer...",
                            "input": {
                                "type": "select",
                                "options": [
                                    {"label": choice, "value": str(index)}
                                    for index, choice in enumerate(choices)
                                ]
                                + [
                                    {
                                        "label": "Other (type your answer)",
                                        "value": "other",
                                    }
                                ],
                            },
                        }
                    )

            response = await __event_call__(event)
            answer = self._parse_answer(response, choices, inline)
            if not inline and choices:
                if answer == "other":
                    response = await __event_call__(
                        {
                            "type": "input",
                            "data": {
                                "title": "Your Answer",
                                "message": question,
                                "placeholder": "Type your answer here...",
                            },
                        }
                    )
                    answer = self._parse_answer(response, [], False)
                elif answer in {str(index) for index in range(len(choices))}:
                    answer = choices[int(answer)]
                else:
                    raise _ClarificationError(
                        "Invalid clarification choice. No answer was received."
                    )
            done_description = "Got your response, continuing..."
            return answer
        except _ClarificationTimeout as exc:
            done_description = "Clarification request timed out."
            return f"Error: {exc}"
        except _ClarificationError as exc:
            return f"Error: {exc}"
        except asyncio.TimeoutError:
            done_description = "Clarification request timed out."
            return f"Error: {_TIMEOUT_MESSAGE}"
        except asyncio.CancelledError:
            done_description = "Clarification request cancelled."
            raise
        except Exception:  # noqa: BLE001 — never expose arbitrary callback errors
            return "Error: Unable to get clarification. Check the browser connection and UI_MODE setting."
        finally:
            await self._emit_status(__event_emitter__, done_description, True)
