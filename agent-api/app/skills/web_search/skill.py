import httpx
from app.skills.base import BaseSkill


class WebSearchSkill(BaseSkill):
    name = "web_search"
    display_name = "Web Search"
    description = "Search the web for current information, news, and facts."
    version = "1.0.0"
    execution_side = "server"
    always_available = True  # Web search could be useful for any query

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": (
                        "Search the web for current information. Runs on the server. "
                        "Use when the user asks about recent events, facts you're unsure about, "
                        "current weather, news, or anything requiring up-to-date information. "
                        "Returns search result snippets with titles."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query. Be specific and include relevant context.",
                            }
                        },
                        "required": ["query"],
                    },
                },
            }
        ]

    async def execute(self, tool_name: str, arguments: dict) -> str:
        query = arguments.get("query", "")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": 1, "no_redirect": 1},
                )
                data = resp.json()
                results = []
                if data.get("AbstractText"):
                    results.append(f"Summary: {data['AbstractText']}")
                for topic in data.get("RelatedTopics", [])[:5]:
                    if isinstance(topic, dict) and "Text" in topic:
                        results.append(f"• {topic['Text']}")
                return "\n".join(results) if results else f"No results for '{query}'. Try a more specific query."
        except httpx.TimeoutException:
            return "Error: Web search timed out after 15s. Try a shorter query."
        except Exception as e:
            return f"Error: Web search failed ({type(e).__name__}). Try again."
