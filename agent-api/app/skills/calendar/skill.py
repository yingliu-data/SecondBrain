from app.skills.base import BaseSkill


class CalendarSkill(BaseSkill):
    name = "calendar"
    display_name = "Calendar"
    description = "Read and create events in the iPhone Calendar app."
    version = "1.0.0"
    execution_side = "device"  # Executes on iPhone via EventKit
    keywords = ["calendar", "event", "schedule", "meeting", "appointment", "busy", "free", "availability"]

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_calendar_events",
                    "description": (
                        "Retrieve upcoming calendar events from the user's iPhone Calendar app. "
                        "Use when the user asks about their schedule, meetings, or availability. "
                        "Returns events with date, time, title, and duration. "
                        "Returns 'No upcoming events' if the calendar is clear."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "days_ahead": {
                                "type": "integer",
                                "description": "Days to look ahead. Use 1 for 'today', 7 for 'this week', 30 for 'this month'. Max 90.",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_calendar_event",
                    "description": (
                        "Create a new event in the user's iPhone Calendar. "
                        "Always confirm details with the user before creating. "
                        "Returns confirmation or error if date format is invalid."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Event title"},
                            "start_date": {"type": "string", "description": "ISO 8601 format (e.g. 2026-02-20T15:00:00)"},
                            "duration_minutes": {"type": "integer", "description": "Duration in minutes. Defaults to 60."},
                        },
                        "required": ["title", "start_date"],
                    },
                },
            },
        ]
    # No execute() — device skills delegate to iPhone automatically
