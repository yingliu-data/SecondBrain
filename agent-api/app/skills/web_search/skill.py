import asyncio
import logging
from app.skills.base import BaseSkill

logger = logging.getLogger("skills.web_search")

MAX_RESULTS = 5


class WebSearchSkill(BaseSkill):
    name = "web_search"
    display_name = "Web Search"
    description = "Search the web for current information, news, and facts."
    version = "1.1.0"
    execution_side = "server"
    always_available = True

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
                        "Returns search result snippets with titles and URLs."
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
        if not query.strip():
            return "Error: Empty search query."

        try:
            from ddgs import DDGS
            from ddgs.exceptions import DDGSException
        except ImportError:
            return "Error: ddgs package is not installed."

        try:
            results = await asyncio.to_thread(
                DDGS().text, query, max_results=MAX_RESULTS
            )
        except DDGSException as e:
            logger.warning("DuckDuckGo search error for %r: %s", query, e)
            return f"Error: Web search failed — {e}. Try rephrasing your query."
        except Exception as e:
            logger.error("Unexpected web search error for %r: %s", query, e)
            return f"Error: Web search failed ({type(e).__name__}). Try again."

        if not results:
            return f"No web results found for '{query}'. Try a broader or different query."

        lines = [f"Web results for \"{query}\":\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            url = r.get("href", "")
            snippet = r.get("body", "")
            lines.append(f"{i}. {title}")
            if url:
                lines.append(f"   {url}")
            if snippet:
                lines.append(f"   {snippet}")

        return "\n".join(lines)
