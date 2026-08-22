"""
title: Ask Clarifying Questions
description: Allows models to ask the user clarifying questions before proceeding. When enabled, the model can call this tool to pause and gather additional information from the user, reducing assumptions and improving response quality.
author: mdelponte
version: 1.1.0
license: MIT
required_open_webui_version: 0.11.0
"""

from pydantic import BaseModel
from typing import Awaitable, Callable, Any, Optional


class Tools:
    class Valves(BaseModel):
        pass

    class UserValves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def ask_clarifying_question(
        self,
        question: str,
        __event_call__: Optional[Callable[[dict], Awaitable[Any]]] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Ask the user a clarifying question and wait for their response.
        Use this tool when the user's request is ambiguous, underspecified,
        or could be interpreted in multiple ways. Call this tool once per
        question. You may call it multiple times in sequence to ask
        several clarifying questions before producing your final answer.
        Examples of good times to use it:
            • The user asks for "a script" but doesn't say what
              language, runtime, or platform.
            • The user asks you to "rewrite this" without saying
              what to change (tone, length, audience, format).
            • The user references something ("the file", "my project",
              "that thing we discussed") that you have no context for.
            • The user asks for a recommendation but hasn't shared
              the constraints (budget, skill level, use case).
            • A task could be done several very different ways and
              the choice meaningfully changes the output.


        :param question: The clarifying question to ask the user.
        :return: The user's response to the question.
        """

        if not __event_call__:
            return "Error: Unable to prompt the user for input in this context."

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": "Waiting for your response...",
                        "done": False,
                    },
                }
            )

        try:
            response = await __event_call__(
                {
                    "type": "input",
                    "data": {
                        "title": "Clarification Needed",
                        "message": question,
                        "placeholder": "Type your answer here...",
                    },
                }
            )
        except Exception as exc:
            # Open WebUI 0.11 can raise when WEBSOCKET_EVENT_CALLER_TIMEOUT
            # expires. Always finish the status so the UI does not keep
            # showing a loading shimmer.
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "Clarification request timed out.",
                            "done": True,
                        },
                    }
                )
            return f"Error: Unable to get clarification from the user ({exc})."

        # A disconnected browser is reported as an error object rather than an
        # exception in Open WebUI 0.11.
        if isinstance(response, dict) and response.get("error"):
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "Clarification request was interrupted.",
                            "done": True,
                        },
                    }
                )
            return f"Error: {response['error']}"

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": "Got your response, continuing...",
                        "done": True,
                    },
                }
            )

        # The browser normally returns the input directly. Keep support for the
        # older {"value": ...} response shape as well.
        if isinstance(response, dict):
            user_answer = response.get("value")
            if user_answer is None:
                user_answer = str(response)
        elif response is None or response == "":
            user_answer = "(No response provided)"
        else:
            user_answer = str(response)

        return str(user_answer)
