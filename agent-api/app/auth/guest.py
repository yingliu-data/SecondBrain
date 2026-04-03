"""Guest session management — unauthenticated, rate-limited, skill-restricted."""

import asyncio
import logging
import time
from collections import defaultdict

logger = logging.getLogger("guest")

# Session lifetime: 60 seconds from first message
SESSION_TTL = 60
# Max messages per session
MAX_MESSAGES = 10
# Max sessions per IP per hour
MAX_SESSIONS_PER_IP_PER_HOUR = 3
# Input character limit
MAX_INPUT_CHARS = 200
# LLM output token cap
MAX_OUTPUT_TOKENS = 200

GUEST_SYSTEM_PROMPT = """You are an avatar controller. You MUST use tools to control the avatar — NEVER just describe a movement in text.

Tool guide:
- plan_movement: Use for clapping, walking, nodding, bowing, waving, shrugging. REQUIRED param: action (walk_cycle, wave_cycle, nod_cycle, clap_cycle, bow_cycle, shrug_cycle). Optional: speed (slow/normal/fast), repeats.
- set_pose: Use for a single static pose. Available poses: t_pose, rest, wave_right, wave_left, hands_up, point_right, point_left, dab, superhero, bow, nod_down, shrug, clap_open, clap_closed, reach_forward_right, reach_forward_left, raise_right_hand, raise_left_hand, raise_right_leg, raise_left_leg.
- animate_sequence: Use for custom multi-pose sequences.
- move_joints: Move individual joints to XYZ coordinates. Only use when no predefined pose matches.

Always prefer set_pose over move_joints. Examples:
- "raise right hand" → set_pose("raise_right_hand")
- "raise left hand" → set_pose("raise_left_hand")
- "raise both hands" / "hands up" → set_pose("hands_up")
- "wave" → plan_movement(action="wave_cycle")
- "raise right leg" → set_pose("raise_right_leg")
- "raise left leg" → set_pose("raise_left_leg")
- "walk" → plan_movement(action="walk_cycle")
- "point right" → set_pose("point_right")

Rules:
- ALWAYS call a tool. Never respond with only text when the user asks for movement.
- Be concise — 1 sentence max after the tool call.
- Decline unrelated requests.
- Current time: {current_time}"""

ALLOWED_ORIGINS = {
    "https://robot.yingliu.site",
    "http://localhost:5173",
    "http://localhost:4173",
}


class GuestSession:
    __slots__ = ("session_id", "ip", "created_at", "message_count", "history")

    def __init__(self, session_id: str, ip: str):
        self.session_id = session_id
        self.ip = ip
        self.created_at = time.time()
        self.message_count = 0
        self.history: list[dict] = []

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > SESSION_TTL

    @property
    def at_message_limit(self) -> bool:
        return self.message_count >= MAX_MESSAGES


class GuestSessionManager:
    def __init__(self):
        self._sessions: dict[str, GuestSession] = {}
        # Track session creation times per IP: ip -> [timestamps]
        self._ip_sessions: dict[str, list[float]] = defaultdict(list)
        self._cleanup_task: asyncio.Task | None = None

    def start_cleanup(self):
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            expired = [sid for sid, s in self._sessions.items()
                       if now - s.created_at > SESSION_TTL + 60]
            for sid in expired:
                del self._sessions[sid]
            # Prune IP history older than 1 hour
            cutoff = now - 3600
            for ip in list(self._ip_sessions):
                self._ip_sessions[ip] = [t for t in self._ip_sessions[ip] if t > cutoff]
                if not self._ip_sessions[ip]:
                    del self._ip_sessions[ip]

    def get_or_create(self, session_id: str, ip: str) -> tuple[GuestSession | None, str]:
        """Return (session, error_message). If error, session is None."""
        if session_id in self._sessions:
            session = self._sessions[session_id]
            if session.expired:
                return None, "Session expired. Please refresh to start a new session."
            if session.at_message_limit:
                return None, "Message limit reached. Please refresh to start a new session."
            return session, ""

        # New session — check IP rate limit
        now = time.time()
        cutoff = now - 3600
        recent = [t for t in self._ip_sessions[ip] if t > cutoff]
        if len(recent) >= MAX_SESSIONS_PER_IP_PER_HOUR:
            return None, "Rate limit: max 3 sessions per hour. Please try again later."

        session = GuestSession(session_id, ip)
        self._sessions[session_id] = session
        self._ip_sessions[ip].append(now)
        return session, ""


guest_manager = GuestSessionManager()
