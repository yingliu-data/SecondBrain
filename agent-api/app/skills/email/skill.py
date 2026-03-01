import json
import logging
import os

from app.skills.base import BaseSkill
from .imap_client import fetch_emails
from .smtp_client import send_reply
from .store import EmailStore

logger = logging.getLogger(__name__)

_EMAIL_ACCOUNTS: list = json.loads(os.environ.get("EMAIL_ACCOUNTS", "[]"))
_EMAIL_DB_PATH: str = os.environ.get("EMAIL_DB_PATH", "data/emails.db")
_SYNC_DAYS_INITIAL: int = int(os.environ.get("EMAIL_SYNC_DAYS_INITIAL", "30"))
_MAX_RESULTS: int = int(os.environ.get("EMAIL_MAX_RESULTS", "10"))


class EmailSkill(BaseSkill):
    name = "email"
    display_name = "Email"
    description = (
        "Read, search, and reply to emails across Gmail, Hotmail, and custom mailboxes. "
        "Junk mail and newsletters are filtered automatically. "
        "Emails are cached locally for fast search."
    )
    version = "1.0.0"
    execution_side = "server"
    keywords = [
        "email", "mail", "inbox", "message", "reply", "sender",
        "gmail", "hotmail", "improx", "mailbox", "unread", "important",
        "wrote", "received", "from", "sent me",
    ]

    def __init__(self):
        self._store = EmailStore(_EMAIL_DB_PATH) if _EMAIL_ACCOUNTS else None

    def get_tool_definitions(self) -> list:
        return [
            {
                "type": "function",
                "function": {
                    "name": "check_important_emails",
                    "description": (
                        "Sync all email accounts and return important unread emails that need a reply. "
                        "Junk mail, newsletters, and automated emails are filtered out automatically. "
                        "Returns a numbered list with sender, date, subject, and preview. "
                        "Each result includes a message_id needed for reply_to_email."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "days_back": {
                                "type": "integer",
                                "description": "How many days back to check. Default 7.",
                            }
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_emails",
                    "description": (
                        "Search the local email database by person name, email address, or topic keyword. "
                        "Fast local search — no IMAP call needed. "
                        "Use when the user asks about emails from a specific person or about a topic or event."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Person name, email address, or keyword to search for.",
                            },
                            "account": {
                                "type": "string",
                                "description": (
                                    "Account to search: 'all', or a specific account name "
                                    "like 'Gmail', 'Hotmail', or 'improx'. Default 'all'."
                                ),
                            },
                            "days_back": {
                                "type": "integer",
                                "description": "How many days back to search. Default 30.",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "reply_to_email",
                    "description": (
                        "Send a reply to an email via SMTP. "
                        "Use message_id and account from a check_important_emails or search_emails result. "
                        "The user provides the reply text as the body."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message_id": {
                                "type": "string",
                                "description": "The Message-ID of the email to reply to.",
                            },
                            "account": {
                                "type": "string",
                                "description": "Account name to send from: e.g. 'Gmail', 'Hotmail', 'improx'.",
                            },
                            "body": {
                                "type": "string",
                                "description": "The reply text to send.",
                            },
                        },
                        "required": ["message_id", "account", "body"],
                    },
                },
            },
        ]

    async def execute(self, tool_name: str, arguments: dict) -> str:
        if not _EMAIL_ACCOUNTS:
            return (
                "Email skill is not configured. "
                "Set the EMAIL_ACCOUNTS environment variable with your mailbox credentials."
            )

        if tool_name == "check_important_emails":
            days_back = int(arguments.get("days_back", 7))
            return await self._check_important(days_back)

        if tool_name == "search_emails":
            query = str(arguments.get("query", "")).strip()
            account = str(arguments.get("account", "all"))
            days_back = int(arguments.get("days_back", 30))
            return await self._search(query, account, days_back)

        if tool_name == "reply_to_email":
            message_id = str(arguments.get("message_id", "")).strip()
            account_name = str(arguments.get("account", "")).strip()
            body = str(arguments.get("body", "")).strip()
            return await self._reply(message_id, account_name, body)

        return f"Unknown tool: {tool_name}"

    async def _check_important(self, days_back: int) -> str:
        total_new = 0
        errors = []

        for account in _EMAIL_ACCOUNTS:
            try:
                since = self._store.get_last_sync(account["name"], _SYNC_DAYS_INITIAL)
                new_emails = await fetch_emails(account, since)
                count = self._store.upsert_emails(new_emails)
                self._store.update_last_sync(account["name"])
                total_new += count
                logger.info("Email sync %s: %d new emails stored", account["name"], count)
            except Exception as e:
                errors.append(f"{account['name']}: {type(e).__name__}")
                logger.warning("Email sync failed for %s: %s", account["name"], e)

        important = self._store.get_important_unread(days_back, _MAX_RESULTS)
        total_unread = self._store.get_total_unread(days_back)
        junk_count = max(0, total_unread - len(important))

        if not important:
            parts = ["No important emails found in the last %d days." % days_back]
            if junk_count > 0:
                parts.append(f"({junk_count} junk/newsletter emails filtered)")
            if errors:
                parts.append(f"Sync errors: {', '.join(errors)}")
            return " ".join(parts)

        account_count = len(_EMAIL_ACCOUNTS)
        lines = [f"IMPORTANT EMAILS ({len(important)} unread, {account_count} accounts synced):"]
        for i, em in enumerate(important, 1):
            lines.append(f"\n{i}. [{em.account}] {em.from_addr} — {em.date} — \"{em.subject}\"")
            if em.body_preview:
                preview = em.body_preview[:120]
                lines.append(f"   → \"{preview}\"")
            lines.append(f"   [message_id: {em.message_id}]")

        footer = []
        if junk_count > 0:
            footer.append(f"{junk_count} junk/newsletter emails filtered")
        if errors:
            footer.append(f"sync errors: {', '.join(errors)}")
        if footer:
            lines.append("\n(" + "; ".join(footer) + ")")

        return "\n".join(lines)

    async def _search(self, query: str, account: str, days_back: int) -> str:
        if not query:
            return "Please provide a search query (person name, email, or topic)."

        try:
            results = self._store.search_fts(query, account, days_back, _MAX_RESULTS)
        except Exception as e:
            return f"Search error: {type(e).__name__}: {e}"

        if not results:
            scope = f"in {account}" if account != "all" else "across all accounts"
            return f"No emails found matching \"{query}\" {scope} in the last {days_back} days."

        scope = f"in {account}" if account != "all" else "across all accounts"
        lines = [
            f"SEARCH RESULTS for \"{query}\" {scope} "
            f"(last {days_back} days, {len(results)} found):"
        ]
        for i, em in enumerate(results, 1):
            lines.append(f"\n{i}. [{em.account}] {em.from_addr} — {em.date} — \"{em.subject}\"")
            if em.body_preview:
                preview = em.body_preview[:120]
                lines.append(f"   → \"{preview}\"")
            lines.append(f"   [message_id: {em.message_id}]")

        return "\n".join(lines)

    async def _reply(self, message_id: str, account_name: str, body: str) -> str:
        if not message_id or not account_name or not body:
            return "Error: message_id, account, and body are all required to send a reply."

        account = next(
            (a for a in _EMAIL_ACCOUNTS if a["name"].lower() == account_name.lower()), None
        )
        if not account:
            available = ", ".join(a["name"] for a in _EMAIL_ACCOUNTS)
            return f"Error: account '{account_name}' not found. Available: {available}"

        original = self._store.get_by_message_id(message_id, account_name)
        if not original:
            return (
                f"Error: email with message_id '{message_id}' not found in local database. "
                "Try running check_important_emails or search_emails first."
            )

        try:
            await send_reply(
                account=account,
                to_addr=original.from_addr,
                subject=original.subject,
                body=body,
                in_reply_to=original.message_id,
                references=original.raw_headers.get("References", ""),
            )
            self._store.mark_replied(message_id, account_name)
            return f"Reply sent to {original.from_addr} \u2713"
        except Exception as e:
            logger.error("SMTP send failed for %s: %s", account_name, e)
            return f"Error sending reply: {type(e).__name__}: {e}"
