"""
title: Ask Clarifying Questions
description: Allows models to ask the user clarifying questions before proceeding. When enabled, the model can call this tool to pause and gather additional information from the user, reducing assumptions and improving response quality.
author: mdelponte
version: 1.0.0
license: MIT
"""

from pydantic import BaseModel, Field
from typing import Callable, Any


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
        __event_call__: Callable[[dict], Any] = None,
        __event_emitter__: Callable[[dict], Any] = None,
    ) -> str:
        """
        Ask the user a clarifying question and wait for their response.
        Use this tool when the user's request is ambiguous, underspecified,
        or could be interpreted in multiple ways. Call this tool once per
        question. You may call it multiple times in sequence to ask
        several clarifying questions before producing your final answer.

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

        # Handle the response — __event_call__ returns the user's input
        # which may be a string directly or a dict with a "value" key
        if isinstance(response, dict):
            user_answer = response.get("value", str(response))
        elif response is None or response == "":
            user_answer = "(No response provided)"
        else:
            user_answer = str(response)

        return user_answer
