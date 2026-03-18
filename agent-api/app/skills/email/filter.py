import re

_NOREPLY_RE = re.compile(
    r"(noreply|no-reply|donotreply|do-not-reply|newsletter|marketing|promo"
    r"|notification|notifications|automated|bounce|mailer-daemon|postmaster)",
    re.IGNORECASE,
)

_BULK_SUBJECT_RE = re.compile(
    r"\b(unsubscribe|newsletter|\d+%\s*off|sale|deals?|offers?|promo|discount"
    r"|coupon|limited\s*time|act\s*now|free\s*shipping|click\s*here)\b",
    re.IGNORECASE,
)


def score_email(headers: dict, folder: str = "INBOX") -> int:
    """
    Score an email for importance. Positive = important, <= 0 = junk/skip.

    Args:
        headers: Dict with keys From, To, Subject, List-Unsubscribe,
                 Precedence, Reply-To, In-Reply-To.
        folder: IMAP folder name (used to detect Spam/Junk folders).

    Returns:
        Integer score. > 0 = show to LLM. <= 0 = discard silently.
    """
    # Instant discard for emails already in a spam/junk folder
    if re.search(r"\b(spam|junk)\b", folder, re.IGNORECASE):
        return -100

    score = 0
    from_addr = headers.get("From", "")
    to_addr = headers.get("To", "")
    subject = headers.get("Subject", "")

    # --- Negative signals ---

    # Mailing list header — almost always a newsletter/subscription
    if headers.get("List-Unsubscribe"):
        score -= 60

    # Sender looks automated (noreply, newsletter, etc.)
    if _NOREPLY_RE.search(from_addr):
        score -= 50

    # Bulk precedence header (RFC 2076)
    precedence = headers.get("Precedence", "").lower().strip()
    if precedence in ("bulk", "list", "junk"):
        score -= 40

    # Subject contains marketing language
    if _BULK_SUBJECT_RE.search(subject):
        score -= 30

    # Reply-To differs from From — typical mailing list / newsletter pattern
    reply_to = headers.get("Reply-To", "").strip()
    if reply_to and reply_to != from_addr.strip():
        score -= 20

    # --- Positive signals ---

    # Direct To: field (not CC/BCC) — personal email
    if to_addr and "@" in to_addr:
        score += 25

    # Part of an existing thread the user participated in
    if headers.get("In-Reply-To", "").strip():
        score += 30

    return score
