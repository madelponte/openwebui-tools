# Ask Clarifying Questions

A simple tool that allows and encourages the model to ask the user clarifying questions if it doesn't understand the prompt well enough. When the model calls the tool, a prompt appears before the user with the question(s) that the model requested clarification on. The model will wait for the user to respond before continuing.

## Installation

1. Go to **Workspace → Tools** and click the **+** button.
2. Paste the contents of `smart_web_search.py` into the editor.
3. Give it a name (e.g. "Smart Web Search") and save.
4. Go to **Workspace → Models**, select your model, click the edit icon.
5. Scroll to the **Tools** section and check "Smart Web Search".
6. In your chat, open **Advanced Params** and set **Function Calling** to **Native**.