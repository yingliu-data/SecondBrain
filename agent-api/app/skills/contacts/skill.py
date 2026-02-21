from app.skills.base import BaseSkill


class ContactsSkill(BaseSkill):
    name = "contacts"
    display_name = "Contacts"
    description = "Search contacts on the user's iPhone."
    version = "1.0.0"
    execution_side = "device"
    keywords = ["contact", "contacts", "phone number", "email", "call", "text", "message"]

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_contacts",
                    "description": (
                        "Search the user's iPhone contacts by name. "
                        "Use when the user asks about a person's phone number, email, or contact info. "
                        "Returns matching contacts with name, phone numbers, and email addresses. "
                        "Returns 'No contacts found' if no match. Try partial names if full name fails."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Name to search for (full or partial)",
                            }
                        },
                        "required": ["name"],
                    },
                },
            }
        ]
