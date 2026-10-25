# Ask Clarifying Questions

A Workspace Tool that lets a model pause and ask the user for missing information, with optional multiple-choice answers. The full selected answer or custom text is returned to the model.

## Compatibility and question UI

- Open WebUI **0.11.0 or later**, with native function/tool calling and a live WebUI browser session. Ordinary API-only callers cannot answer interactive prompts.
- **0.11.1+:** questions appear in the built-in inline panel above the chat input. Click an answer or type a custom response in **Other**. Open-ended questions show just the text entry.
- **0.11.0:** uses the floating input dialog. Choices appear in a dropdown; selecting **Other (type your answer)** opens a second dialog for custom text.

Third-party tools can use the inline panel: `__event_call__` with event type `request:user_input` reaches the same UI as the built-in `ask_user`, without invoking that tool's strict argument validation. This tool generates question IDs, headers, and option objects itself; the model only supplies a question and optional strings.

Verified against upstream [0.11.1 event handling](https://github.com/open-webui/open-webui/blob/v0.11.1/src/lib/components/chat/Chat.svelte), [inline card](https://github.com/open-webui/open-webui/blob/v0.11.1/src/lib/components/chat/AskUserCard.svelte), and [0.11.3 built-in implementation](https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/tools/builtin.py). This is a frontend event integration, not a guarantee that custom tools inherit every built-in feature: prompts require the original live session and are not restored after a page reload.

## Installation / update

1. Go to **Workspace → Tools**.
2. Create a tool, or edit your existing **Ask Clarifying Questions** tool.
3. Paste the contents of `ask_clarifying_questions.py`, then save it.
4. In **Workspace → Models**, edit the model that should use the tool.
5. Add **Ask Clarifying Questions** in the model's **Tools** section and save.
6. To use this instead of the built-in tool, disable **Ask User** under that model's **Builtin Tools** settings. This does not disable the inline UI used by this tool.

Native function calling is the default in Open WebUI 0.11.0. If the model has an explicit override, ensure **Function Calling** is set to **Native**, not Legacy.

## Model-facing arguments

The method remains `ask_clarifying_question`; existing question-only calls still work.

Open-ended:

```json
{"question": "Which operating system are you using?"}
```

Multiple-choice (single selection, with custom text always available):

```json
{
  "question": "Which language should I use?",
  "choices": ["Python", "JavaScript", "Go", "Rust"]
}
```

There is no required option count and no need for labels/descriptions in nested objects. Omitted, null, or empty choices produce an open-ended question. Blank strings and duplicate choices are removed. Answers are not truncated. The upstream inline UI labels the **first option as Recommended**, so the model is instructed to put its preferred choice first.

Ask one question per call and call sequentially, not in parallel: WebUI's interactive event UI shares a single active callback. Cancellation, empty answers, and interaction errors are returned explicitly rather than treated as a selection.

## Valves

| Valve | Default | Behavior |
| --- | --- | --- |
| `UI_MODE` | `auto` | Detects the server version: inline on 0.11.1+, modal on older/unknown versions. `modal` forces the floating dialog; `inline` forces the modern panel for custom/backported builds. |

The browser and server should run matching versions. If prompts do not appear after an upgrade, refresh the browser and check `UI_MODE`. Do not force `inline` on an unsupported frontend: unknown events may wait without displaying anything. There is no automatic retry after a timeout or cancellation, which could otherwise cause duplicate prompts.

## Waiting and errors

Like the original tool, prompts have no tool-imposed deadline. Administrators can configure Open WebUI's `WEBSOCKET_EVENT_CALLER_TIMEOUT` to bound the server wait. The tool handles timeout exceptions, disconnected-client error responses, and cancellation, and finishes progress statuses on handled failures. Failed status delivery does not discard a successful answer; raw backend exceptions are not exposed to the model.

## Offline validation

With Pydantic installed, run from the repository root:

```bash
python3 -m unittest discover -s ask_clarifying_questions/tests -v
```

These tests mock browser callbacks and server-version detection; they do not replace a browser smoke test on your WebUI deployment.
