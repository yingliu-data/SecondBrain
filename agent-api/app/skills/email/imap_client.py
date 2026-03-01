"""
Async IMAP client for fetching emails.

Uses Python's standard imaplib wrapped with asyncio.to_thread so it doesn't
block the event loop. All IMAP I/O happens in a thread pool worker.
"""
import asyncio
import email as email_lib
import imaplib
import logging
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime

from .filter import score_email
from .store import Email

logger = logging.getLogger(__name__)

_PREVIEW_LEN = 400
_MAX_UIDS_PER_FOLDER = 300  # Cap per folder to avoid very slow syncs


def _decode_header_value(value: str) -> str:
    """Decode RFC 2047 encoded header values to plain Unicode."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(part.decode("latin-1", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded).strip()


def _extract_body_preview(raw_bytes: bytes) -> str:
    """Parse a raw RFC822 message and extract a plain-text body preview."""
    try:
        msg = email_lib.message_from_bytes(raw_bytes)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="replace")
                        break
                    except Exception:
                        continue
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
            except Exception:
                pass
        body = re.sub(r"\s+", " ", body).strip()
        return body[:_PREVIEW_LEN]
    except Exception:
        return ""


def _get_folders_to_check(imap: imaplib.IMAP4_SSL) -> list:
    """Return list of IMAP folder names to check: INBOX + any Spam/Junk folders."""
    folders = ["INBOX"]
    try:
        _, folder_list = imap.list()
        for item in folder_list or []:
            raw = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else item
            # IMAP LIST format: (\Flags) "/" "Folder Name"
            # Extract the folder name from the end of the line
            m = re.search(r'"([^"]+)"\s*$', raw)
            if m:
                folder_name = m.group(1)
            else:
                # Unquoted folder name
                parts = raw.rsplit(" ", 1)
                folder_name = parts[-1].strip().strip('"')
            if re.search(r"\b(spam|junk)\b", folder_name, re.IGNORECASE):
                if folder_name not in folders:
                    folders.append(folder_name)
    except Exception as e:
        logger.debug("Could not list IMAP folders: %s", e)
    return folders


def _fetch_emails_sync(account: dict, since_date: datetime) -> list:
    """
    Synchronous IMAP fetch. Runs inside a thread pool via asyncio.to_thread.

    Phase 1: Fetch headers for all emails since since_date, score for junk.
    Phase 2: Fetch body preview only for emails with importance > 0.
    """
    emails = []
    since_str = since_date.strftime("%d-%b-%Y")  # IMAP date format: 01-Mar-2026

    imap = imaplib.IMAP4_SSL(account["imap_host"], account.get("imap_port", 993))
    imap.socket().settimeout(30)
    try:
        imap.login(account["user"], account["password"])
        folders = _get_folders_to_check(imap)

        for folder in folders:
            folder_quoted = f'"{folder}"' if " " in folder else folder
            try:
                status, _ = imap.select(folder_quoted, readonly=True)
                if status != "OK":
                    continue
            except Exception as e:
                logger.debug("Cannot select folder %s: %s", folder, e)
                continue

            _, msg_nums = imap.search(None, f"SINCE {since_str}")
            if not msg_nums or not msg_nums[0]:
                continue

            uid_list = msg_nums[0].split()[-_MAX_UIDS_PER_FOLDER:]  # Most recent N

            # Phase 1: Fetch headers only — fast, tiny payloads
            header_fields = (
                "FROM TO CC SUBJECT DATE MESSAGE-ID "
                "IN-REPLY-TO REFERENCES LIST-UNSUBSCRIBE PRECEDENCE REPLY-TO"
            )
            pending = []  # (uid, Email) for phase 2 body fetch
            for uid in uid_list:
                try:
                    _, data = imap.fetch(uid, f"(BODY.PEEK[HEADER.FIELDS ({header_fields})])")
                    if not data or not data[0] or not isinstance(data[0], tuple):
                        continue
                    msg = email_lib.message_from_bytes(data[0][1])
                except Exception as e:
                    logger.debug("Header fetch error uid=%s: %s", uid, e)
                    continue

                hdrs = {
                    "From": _decode_header_value(msg.get("From", "")),
                    "To": _decode_header_value(msg.get("To", "")),
                    "Subject": _decode_header_value(msg.get("Subject", "")),
                    "List-Unsubscribe": msg.get("List-Unsubscribe", ""),
                    "Precedence": msg.get("Precedence", ""),
                    "Reply-To": msg.get("Reply-To", ""),
                    "In-Reply-To": msg.get("In-Reply-To", ""),
                    "References": msg.get("References", ""),
                }
                importance = score_email(hdrs, folder)

                message_id = msg.get("Message-ID", "").strip()
                if not message_id:
                    message_id = f"<uid-{uid.decode()}-{account['name']}>"

                date_str = msg.get("Date", "")
                try:
                    iso_date = parsedate_to_datetime(date_str).strftime("%Y-%m-%d")
                except Exception:
                    iso_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                em = Email(
                    message_id=message_id,
                    account=account["name"],
                    folder=folder,
                    from_addr=hdrs["From"],
                    to_addr=hdrs["To"],
                    subject=hdrs["Subject"],
                    date=iso_date,
                    body_preview="",
                    raw_headers=hdrs,
                    importance=importance,
                    is_read=False,
                )
                if importance > 0:
                    pending.append((uid, em))
                else:
                    emails.append(em)

            # Phase 2: Fetch body preview only for important emails
            for uid, em in pending:
                try:
                    # Fetch first 4000 bytes of raw message for body extraction
                    _, data = imap.fetch(uid, "(BODY.PEEK[]<0.4000>)")
                    if data and data[0] and isinstance(data[0], tuple):
                        em.body_preview = _extract_body_preview(data[0][1])
                except Exception as e:
                    logger.debug("Body fetch error uid=%s: %s", uid, e)
                emails.append(em)

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return emails


async def fetch_emails(account: dict, since_date: datetime) -> list:
    """Async wrapper: fetch emails from one IMAP account since the given date."""
    return await asyncio.to_thread(_fetch_emails_sync, account, since_date)
