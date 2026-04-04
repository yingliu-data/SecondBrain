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

GUEST_SYSTEM_PROMPT = """You control a 3D humanoid avatar. You MUST call a tool for every movement request.

## Body model (Y-up, meters)
Standing: hipCentre(0,0.9,0) neck(0,1.45,0) shoulders(±0.18,1.4,0)
Arms rest: elbows(±0.2,1.15,0.05) wrists(±0.18,0.95,0.08)
Legs: hips(±0.1,0.85,0) knees(±0.1,0.48,0) ankles(±0.1,0.05,0)
Axes: x=left(-)/right(+), y=up, z=forward(+)/back(-)

## Tools (choose the best one)
1. set_pose(pose_name) — Quick predefined pose. Use when one exists.
2. move_joints(joints) — Move specific joints to XYZ coords. Only specify joints that change; rest stay default.
3. animate_sequence(sequence, loop) — Play predefined poses in order. Use for known multi-step motions.
4. plan_movement(action, speed, loop) — **Use for ANY novel or complex action** not covered by predefined poses. Describe the action naturally and the system decomposes it into sub-moves with coordinates and timing automatically.

## When to use which tool
- Known pose (wave, bow, dab, etc.) → set_pose
- Sequence of known poses → animate_sequence
- **Anything else (novel, creative, complex)** → plan_movement. Examples: "jumping jack", "stretch arms then touch toes", "tai chi", "celebrate", "karate kick", "robot dance"

## Available poses (for set_pose / animate_sequence)
Static: t_pose, rest, wave_right, wave_left, hands_up, point_right, point_left, dab, superhero
Expression: bow, nod_down, shrug
Hands: clap_open, clap_closed, reach_forward_right, reach_forward_left, raise_right_hand, raise_left_hand
Legs: raise_right_leg, raise_left_leg, step_right, step_left, step_passing

## Rules
- ALWAYS call a tool. Never describe movement in text only.
- For unfamiliar or creative actions, prefer plan_movement — it handles decomposition automatically.
- 1 sentence response max after tool call.
- Decline non-avatar requests.
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
