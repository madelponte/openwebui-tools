"""Offline contract tests; no Open WebUI server or browser is required."""

import asyncio
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import create_model

SPEC = importlib.util.spec_from_file_location(
    "clarification_plugin",
    Path(__file__).resolve().parents[1] / "ask_clarifying_questions.py",
)
assert SPEC is not None and SPEC.loader is not None
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)
Tools = PLUGIN.Tools


def inline_response(answer):
    return {"status": "answered", "answers": {"clarification": answer}}


class ClarificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = Tools()
        self.tool.valves.UI_MODE = "inline"

    async def invoke(self, response, **kwargs):
        caller = AsyncMock(return_value=response)
        emitter = AsyncMock()
        result = await self.tool.ask_clarifying_question(
            "Which language?",
            __event_call__=caller,
            __event_emitter__=emitter,
            **kwargs,
        )
        self.assertEqual(
            [call.args[0]["data"]["done"] for call in emitter.call_args_list],
            [False, True],
        )
        return result, caller

    async def test_inline_choices_and_complete_label(self):
        label = "A very long choice " * 30
        result, caller = await self.invoke(
            inline_response(
                {"type": "option", "option_index": 3, "label": "wrong echo"}
            ),
            choices=["Python", "Go", "Rust", label],
        )
        self.assertEqual(result, label.strip())
        event = caller.call_args.args[0]
        self.assertEqual(event["type"], "request:user_input")
        self.assertIsNone(event["data"]["timeout_ms"])
        self.assertTrue(event["data"]["allow_other"])
        question = event["data"]["questions"][0]
        self.assertEqual(question["id"], "clarification")
        self.assertEqual(question["question"], "Which language?")
        self.assertEqual(len(question["options"]), 4)
        self.assertEqual(question["options"][0], {"label": "Python", "description": ""})

    async def test_inline_open_ended_and_custom_text(self):
        for choices in (None, [], ["Python", "Go"]):
            with self.subTest(choices=choices):
                result, caller = await self.invoke(
                    inline_response({"type": "other", "text": "  C++\nwith Qt  "}),
                    choices=choices,
                )
                self.assertEqual(result, "C++\nwith Qt")
                self.assertEqual(
                    len(caller.call_args.args[0]["data"]["questions"][0]["options"]),
                    len(choices or []),
                )

    async def test_forgiving_choices(self):
        for choices in ([" Python ", "", "Python", "  ", None], "Python"):
            result, caller = await self.invoke(
                inline_response({"type": "option", "option_index": 0}), choices=choices
            )
            self.assertEqual(result, "Python")
            self.assertEqual(
                len(caller.call_args.args[0]["data"]["questions"][0]["options"]), 1
            )

    async def test_modal_question_only_and_legacy_response(self):
        self.tool.valves.UI_MODE = "modal"
        for response in ("Linux", {"value": "Linux"}):
            result, caller = await self.invoke(response)
            self.assertEqual(result, "Linux")
            event = caller.call_args.args[0]
            self.assertEqual(event["type"], "input")
            self.assertNotIn("input", event["data"])

    async def test_modal_selection_returns_original_text(self):
        self.tool.valves.UI_MODE = "modal"
        result, caller = await self.invoke(
            {"value": "1"}, choices=["other", "<Rust & Go>"]
        )
        self.assertEqual(result, "<Rust & Go>")
        options = caller.call_args.args[0]["data"]["input"]["options"]
        self.assertEqual(options[0], {"label": "other", "value": "0"})
        self.assertEqual(options[-1]["value"], "other")

    async def test_modal_other_followup(self):
        self.tool.valves.UI_MODE = "modal"
        for response, expected in (({"value": "C++"}, "C++"), (None, "Error:")):
            with self.subTest(response=response):
                caller = AsyncMock(side_effect=["other", response])
                emitter = AsyncMock()
                result = await self.tool.ask_clarifying_question(
                    "Language?",
                    choices=["Python"],
                    __event_call__=caller,
                    __event_emitter__=emitter,
                )
                self.assertTrue(result.startswith(expected))
                self.assertEqual(caller.await_count, 2)
                self.assertNotIn("input", caller.call_args.args[0]["data"])
                self.assertTrue(emitter.call_args.args[0]["data"]["done"])

    async def test_unanswered_and_malformed_inline_responses(self):
        responses = [
            None,
            False,
            "",
            "raw string",
            {},
            {"value": "legacy"},
            {"status": "cancelled", "answers": {}},
            {"error": "Client session disconnected."},
            {"error": "Event call timed out."},
            {"status": "answered", "answers": []},
            inline_response({"type": "other", "text": "  "}),
            inline_response({"type": "other", "text": 42}),
            inline_response({"type": "unknown"}),
        ]
        responses += [
            inline_response({"type": "option", "option_index": index})
            for index in (-1, 1, True, "0", None)
        ]
        for response in responses:
            with self.subTest(response=response):
                result, caller = await self.invoke(response, choices=["Python"])
                self.assertTrue(result.startswith("Error:"))
                self.assertEqual(caller.await_count, 1)

    async def test_modal_cancel_empty_and_invalid(self):
        self.tool.valves.UI_MODE = "modal"
        for response in (
            None,
            False,
            "",
            "  ",
            {},
            {"value": None},
            {"value": ""},
            "99",
        ):
            with self.subTest(response=response):
                result, caller = await self.invoke(response, choices=["Python"])
                self.assertTrue(result.startswith("Error:"))
                self.assertEqual(caller.await_count, 1)

    async def test_exceptions_are_safe_and_finish_status(self):
        secret = "https://user:secret@example.invalid/?token=private"
        for exc in (
            asyncio.TimeoutError(secret),
            RuntimeError(secret),
            ValueError(secret),
        ):
            with self.subTest(exc=type(exc)):
                caller = AsyncMock(side_effect=exc)
                emitter = AsyncMock()
                result = await self.tool.ask_clarifying_question(
                    "Language?", __event_call__=caller, __event_emitter__=emitter
                )
                self.assertTrue(result.startswith("Error:"))
                self.assertNotIn(secret, result + str(emitter.call_args_list))
                self.assertTrue(emitter.call_args.args[0]["data"]["done"])
        result, _ = await self.invoke({"error": secret})
        self.assertNotIn(secret, result)

    async def test_failed_status_delivery_preserves_answer(self):
        caller = AsyncMock(
            return_value=inline_response({"type": "other", "text": "Python"})
        )
        emitter = AsyncMock(side_effect=RuntimeError("Disconnected"))
        result = await self.tool.ask_clarifying_question(
            "Language?", __event_call__=caller, __event_emitter__=emitter
        )
        self.assertEqual(result, "Python")
        self.assertEqual(emitter.await_count, 2)

    async def test_missing_emitter(self):
        caller = AsyncMock(
            return_value=inline_response({"type": "other", "text": "Python"})
        )
        self.assertEqual(
            await self.tool.ask_clarifying_question("Language?", __event_call__=caller),
            "Python",
        )

    async def test_task_cancellation_propagates_and_finishes_status(self):
        emitter = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await self.tool.ask_clarifying_question(
                "Language?",
                __event_call__=AsyncMock(side_effect=asyncio.CancelledError),
                __event_emitter__=emitter,
            )
        self.assertTrue(emitter.call_args.args[0]["data"]["done"])

    async def test_invalid_question_or_missing_session(self):
        caller = AsyncMock()
        for question in ("", "   ", None):
            result = await self.tool.ask_clarifying_question(
                question, __event_call__=caller
            )
            self.assertTrue(result.startswith("Error:"))
        caller.assert_not_called()
        self.assertTrue(
            (await self.tool.ask_clarifying_question("Language?")).startswith("Error:")
        )

    def test_version_detection_and_overrides(self):
        self.tool.valves.UI_MODE = "auto"
        env = types.ModuleType("open_webui.env")
        package = types.ModuleType("open_webui")
        with patch.dict(sys.modules, {"open_webui": package, "open_webui.env": env}):
            for version, expected in (
                ("0.11.0", False),
                ("0.11.1", True),
                ("0.11.3", True),
                ("0.12.0", True),
                ("1.0.0", True),
                ("unknown", False),
            ):
                with self.subTest(version=version):
                    env.__dict__["VERSION"] = version
                    self.assertEqual(self.tool._use_inline(), expected)
            self.tool.valves.UI_MODE = "inline"
            env.__dict__["VERSION"] = "0.11.0"
            self.assertTrue(self.tool._use_inline())
            self.tool.valves.UI_MODE = "modal"
            env.__dict__["VERSION"] = "0.11.3"
            self.assertFalse(self.tool._use_inline())
        self.tool.valves.UI_MODE = "auto"
        with patch.dict(sys.modules, {"open_webui.env": None}):
            self.assertFalse(self.tool._use_inline())

    def test_model_facing_signature_is_simple(self):
        public = [
            name
            for name, member in inspect.getmembers(self.tool, inspect.ismethod)
            if not name.startswith("_")
        ]
        self.assertEqual(public, ["ask_clarifying_question"])
        fields = {}
        for name, param in inspect.signature(
            self.tool.ask_clarifying_question
        ).parameters.items():
            if not name.startswith("__"):
                fields[name] = (
                    param.annotation,
                    ... if param.default is inspect.Parameter.empty else param.default,
                )
        schema = create_model("ClarificationArguments", **fields).model_json_schema()
        self.assertEqual(schema["required"], ["question"])
        self.assertEqual(set(schema["properties"]), {"question", "choices"})
        self.assertIn(
            {"type": "array", "items": {"type": "string"}},
            schema["properties"]["choices"]["anyOf"],
        )


if __name__ == "__main__":
    unittest.main()
