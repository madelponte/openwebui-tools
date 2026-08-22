# Ask Clarifying Questions

A Workspace Tool that lets a model pause and ask the user for missing information. The user's answer is returned to the model so it can continue with fewer assumptions.

## Compatibility

- Open WebUI **0.11.0 or later**
- A model with native function/tool calling support
- A live WebUI chat session (interactive prompts are not available to ordinary API-only callers)

## Installation

1. Go to **Workspace → Tools**.
2. Open **Create** and create a new tool.
3. Paste the contents of `ask_clarifying_questions.py`, then save it.
4. In **Workspace → Models**, edit the model that should use the tool.
5. Add **Ask Clarifying Questions** in the model's **Tools** section and save.

Native function calling is the default in Open WebUI 0.11.0. If the model has an explicit override, ensure **Function Calling** is set to **Native**, not Legacy.

## Notes

Open WebUI's `__event_call__` waits indefinitely by default. Administrators can configure `WEBSOCKET_EVENT_CALLER_TIMEOUT` to bound the wait. This tool handles both timeout exceptions and the disconnected-client response introduced in current Open WebUI versions.
