from app.skills.base import BaseSkill


class RemindersSkill(BaseSkill):
    name = "reminders"
    display_name = "Reminders"
    description = "Read and create reminders in the iPhone Reminders app."
    version = "1.0.0"
    execution_side = "device"
    keywords = ["reminder", "remind", "todo", "task", "to-do", "to do"]

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_reminders",
                    "description": (
                        "Get pending reminders from the user's iPhone Reminders app. "
                        "Use when the user asks about their tasks, to-dos, or reminders. "
                        "Returns up to 20 incomplete reminders with titles. "
                        "Returns 'No pending reminders' if the list is clear."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_reminder",
                    "description": (
                        "Create a new reminder in the user's iPhone Reminders app. "
                        "Always confirm the reminder title with the user before creating. "
                        "Returns confirmation with title and optional due date."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Reminder title"},
                            "due_date": {"type": "string", "description": "Optional ISO 8601 due date (e.g. 2026-02-21T09:00:00)"},
                        },
                        "required": ["title"],
                    },
                },
            },
        ]
