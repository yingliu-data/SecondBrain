from app.skills.base import BaseSkill


class ClipboardSkill(BaseSkill):
    name = "clipboard"
    display_name = "Clipboard"
    description = "Read the contents of the iPhone clipboard."
    version = "1.0.0"
    execution_side = "device"
    keywords = ["clipboard", "paste", "copied", "copy"]

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_clipboard",
                    "description": (
                        "Read the current contents of the user's iPhone clipboard. "
                        "Use when the user asks about what they copied or what's on their clipboard. "
                        "Returns the clipboard text or 'Clipboard is empty' if nothing is copied."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
        ]
