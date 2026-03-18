"""
Async SMTP client for sending email replies.

Uses Python's standard smtplib wrapped with asyncio.to_thread so it doesn't
block the event loop.
"""
import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid


def _send_reply_sync(
    account: dict,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
) -> None:
    """
    Synchronous SMTP send. Runs in a thread pool via asyncio.to_thread.

    Raises smtplib exceptions on failure — caller should catch and handle.
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = account["user"]
    msg["To"] = to_addr
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid()

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        ref = f"{references} {in_reply_to}".strip() if references else in_reply_to
        msg["References"] = ref

    msg.attach(MIMEText(body, "plain", "utf-8"))

    smtp_host = account.get("smtp_host", account["imap_host"].replace("imap.", "smtp."))
    smtp_port = int(account.get("smtp_port", 587))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(account["user"], account["password"])
        server.sendmail(account["user"], [to_addr], msg.as_string())


async def send_reply(
    account: dict,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
) -> None:
    """Async wrapper: send a reply email via SMTP."""
    await asyncio.to_thread(
        _send_reply_sync, account, to_addr, subject, body, in_reply_to, references
    )
