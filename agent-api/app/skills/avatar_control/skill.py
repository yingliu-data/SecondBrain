import json
import logging
from app.skills.base import BaseSkill
from app.skills.avatar_control.poses import POSES, POSE_NAMES
from app.skills.avatar_control.body import JOINT_LIMITS, clamp_joint
from app.skills.avatar_control.motion import build_animation, EasingType

logger = logging.getLogger("skills.avatar_control")

VALID_JOINTS = {
    "hipCentre", "neck",
    "leftHip", "leftKnee", "leftAnkle", "leftToe",
    "rightHip", "rightKnee", "rightAnkle", "rightToe",
    "leftShoulder", "leftElbow", "leftWrist",
    "rightShoulder", "rightElbow", "rightWrist",
}

# Prompt for the secondary LLM call that decomposes actions into keyframes.
_PLANNER_PROMPT = """You are a motion planner for a 3D humanoid avatar. Decompose the requested action into a sequence of keyframe poses.

## Body model (Y-up, meters)
Joint names: hipCentre, neck, leftHip, leftKnee, leftAnkle, leftToe, rightHip, rightKnee, rightAnkle, rightToe, leftShoulder, leftElbow, leftWrist, rightShoulder, rightElbow, rightWrist

Standing rest coordinates:
- hipCentre(0, 0.9, 0), neck(0, 1.45, 0)
- shoulders: left(-0.18, 1.4, 0), right(0.18, 1.4, 0)
- elbows: left(-0.2, 1.15, 0.05), right(0.2, 1.15, 0.05)
- wrists: left(-0.18, 0.95, 0.08), right(0.18, 0.95, 0.08)
- hips: left(-0.1, 0.85, 0), right(0.1, 0.85, 0)
- knees: left(-0.1, 0.48, 0), right(0.1, 0.48, 0)
- ankles: left(-0.1, 0.05, 0), right(0.1, 0.05, 0)
- toes: left(-0.1, 0.0, 0.08), right(0.1, 0.0, 0.08)

Axes: x = left(-)/right(+), y = up(+), z = forward(+)/back(-)

## Joint limits (min, max per axis)
{limits}

## Rules
- Output ONLY valid JSON, no explanation.
- Include ALL 16 joints in every keyframe.
- Keep coordinates within the joint limits above.
- Use 3-8 keyframes. More keyframes = smoother motion.
- duration_ms: time to transition TO this keyframe (200-1000ms).
- Start from rest pose. End at rest unless loop is true.
- Think physically: arms swing opposite to legs, weight shifts, etc.

## Output format
{{"keyframes": [{{"joints": {{"hipCentre": {{"x": 0, "y": 0.9, "z": 0}}, ...all 16 joints...}}, "duration_ms": 500}}, ...], "loop": false}}"""

# Compact limits string for the planner prompt
_LIMITS_STR = "\n".join(
    f"  {j}: x({lim['x'][0]:.2f}, {lim['x'][1]:.2f}) "
    f"y({lim['y'][0]:.2f}, {lim['y'][1]:.2f}) "
    f"z({lim['z'][0]:.2f}, {lim['z'][1]:.2f})"
    for j, lim in JOINT_LIMITS.items()
)


class AvatarControlSkill(BaseSkill):
    name = "avatar_control"
    display_name = "Avatar Control"
    description = "Control a 3D avatar's body pose and animations."
    version = "4.0.0"
    execution_side = "server"
    keywords = [
        "avatar", "robot", "control", "move", "pose", "arm", "leg",
        "hand", "wave", "dance", "raise", "point", "gesture",
        "walk", "step", "clap", "nod", "bow", "shrug", "reach",
        "jump", "kick", "stretch", "sit", "squat", "spin", "turn",
    ]

    def __init__(self):
        self._llm = None

    def set_llm(self, llm):
        self._llm = llm

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "set_pose",
                    "description": (
                        "Set the avatar to a predefined full-body pose."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pose_name": {
                                "type": "string",
                                "enum": POSE_NAMES,
                                "description": "Name of the predefined pose.",
                            },
                        },
                        "required": ["pose_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "move_joints",
                    "description": (
                        "Move specific joints to 3D positions. Only specify "
                        "joints you want to change — others stay at rest. "
                        "Y-up: x=left(-)/right(+), y=up, z=forward(+)/back(-)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "joints": {
                                "type": "object",
                                "description": (
                                    "Joint name -> {x, y, z}. "
                                    "Valid: " + ", ".join(sorted(VALID_JOINTS))
                                ),
                                "additionalProperties": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "number"},
                                        "y": {"type": "number"},
                                        "z": {"type": "number"},
                                    },
                                    "required": ["x", "y", "z"],
                                },
                            },
                        },
                        "required": ["joints"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "animate_sequence",
                    "description": (
                        "Play a sequence of poses as animation. Use for any "
                        "multi-step movement: walking, dancing, clapping, etc. "
                        "Set loop=true for repeating motions."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sequence": {
                                "type": "array",
                                "description": "Pose steps to play in order.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "pose_name": {
                                            "type": "string",
                                            "enum": POSE_NAMES,
                                        },
                                        "hold_ms": {
                                            "type": "integer",
                                            "description": "Hold duration in ms.",
                                            "default": 400,
                                        },
                                    },
                                    "required": ["pose_name"],
                                },
                            },
                            "loop": {
                                "type": "boolean",
                                "description": "Repeat the animation.",
                                "default": False,
                            },
                        },
                        "required": ["sequence"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "plan_movement",
                    "description": (
                        "Plan and execute ANY movement from a natural language "
                        "description. Use this when the action is NOT a simple "
                        "predefined pose or sequence. The system decomposes the "
                        "action into sub-moves, constructs joint coordinates, "
                        "assigns timing, and produces smooth animation. "
                        "Examples: 'jumping jack', 'stretch arms overhead then "
                        "touch toes', 'tai chi wave', 'celebrate with fist pump'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": (
                                    "Natural language description of the movement "
                                    "to perform. Be specific about what body parts "
                                    "move and how."
                                ),
                            },
                            "speed": {
                                "type": "string",
                                "enum": ["slow", "normal", "fast"],
                                "description": "Movement speed. Default: normal.",
                            },
                            "loop": {
                                "type": "boolean",
                                "description": "Repeat the animation.",
                                "default": False,
                            },
                        },
                        "required": ["action"],
                    },
                },
            },
        ]

    async def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "set_pose":
            return self._set_pose(arguments)
        elif tool_name == "move_joints":
            return self._move_joints(arguments)
        elif tool_name == "animate_sequence":
            return self._animate_sequence(arguments)
        elif tool_name == "plan_movement":
            return await self._plan_movement(arguments)
        return f"Error: Unknown tool '{tool_name}'"

    def _set_pose(self, args: dict) -> str:
        pose_name = args.get("pose_name", "")
        if pose_name not in POSES:
            return f"Error: Unknown pose '{pose_name}'. Available: {', '.join(POSE_NAMES)}"
        return json.dumps({
            "type": "pose",
            "joints": POSES[pose_name],
            "duration_ms": args.get("duration_ms", 500),
        })

    def _move_joints(self, args: dict) -> str:
        joints = args.get("joints", {})
        invalid = set(joints.keys()) - VALID_JOINTS
        if invalid:
            return f"Error: Invalid joints: {', '.join(invalid)}. Valid: {', '.join(sorted(VALID_JOINTS))}"
        full_pose = {k: dict(v) for k, v in POSES["rest"].items()}
        for joint_name, coords in joints.items():
            full_pose[joint_name] = dict(coords)
        return json.dumps({
            "type": "pose",
            "joints": full_pose,
            "duration_ms": args.get("duration_ms", 500),
        })

    def _animate_sequence(self, args: dict) -> str:
        sequence = args.get("sequence", [])
        if not sequence:
            return "Error: Empty sequence."
        frames = []
        for step in sequence:
            pose_name = step.get("pose_name", "")
            if pose_name not in POSES:
                return f"Error: Unknown pose '{pose_name}' in sequence."
            frames.append({
                "joints": POSES[pose_name],
                "hold_ms": step.get("hold_ms", 400),
            })
        return json.dumps({
            "type": "animation",
            "frames": frames,
            "loop": args.get("loop", False),
        })

    async def _plan_movement(self, args: dict) -> str:
        action = args.get("action", "")
        if not action:
            return "Error: action is required."
        if not self._llm:
            return "Error: LLM not available for motion planning."

        speed = args.get("speed", "normal")
        loop = args.get("loop", False)

        # Speed maps to transition duration
        speed_ms = {"slow": 800, "normal": 500, "fast": 300}.get(speed, 500)

        # Build the planner prompt
        system_prompt = _PLANNER_PROMPT.format(limits=_LIMITS_STR)
        user_prompt = f"Action: {action}\nSpeed: {speed}\nLoop: {loop}"

        try:
            resp = await self._llm.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tools=None,
            )
            raw = resp["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"plan_movement LLM call failed: {e}")
            return "Error: Motion planning failed. Try a simpler action or use set_pose/animate_sequence."

        # Parse the LLM response as JSON
        plan = self._parse_plan(raw)
        if isinstance(plan, str):
            return plan  # error message

        keyframes_data = plan.get("keyframes", [])
        if not keyframes_data:
            return "Error: Planner produced no keyframes."

        # Validate, clamp, and extract keyframe poses
        keyframe_poses = []
        for i, kf in enumerate(keyframes_data):
            joints = kf.get("joints", {})
            invalid = set(joints.keys()) - VALID_JOINTS
            if invalid:
                logger.warning(f"Keyframe {i}: removing invalid joints {invalid}")
                joints = {k: v for k, v in joints.items() if k in VALID_JOINTS}

            # Merge onto rest pose to fill missing joints, then clamp
            full = {k: dict(v) for k, v in POSES["rest"].items()}
            for jname, coords in joints.items():
                full[jname] = clamp_joint(jname, coords)
            keyframe_poses.append(full)

        # Use motion engine to interpolate between keyframes
        frames = build_animation(
            keyframe_poses,
            default_easing=EasingType.EASE_IN_OUT,
            default_duration_ms=speed_ms,
            frame_interval_ms=80,
        )

        if not frames:
            return "Error: Interpolation produced no frames."

        result = {
            "type": "animation",
            "frames": frames,
            "loop": plan.get("loop", loop),
        }

        # Return full animation for the frontend SSE, but compact summary
        # will be fed to the LLM by the agent loop (via _AVATAR_TOOLS handling)
        logger.info(f"plan_movement: '{action}' -> {len(keyframe_poses)} keyframes, {len(frames)} frames")
        return json.dumps(result)

    def _parse_plan(self, raw: str) -> dict | str:
        """Extract JSON from LLM response, handling markdown fences."""
        text = raw.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last fence lines
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"plan_movement: Failed to parse planner JSON: {e}\nRaw: {raw[:500]}")
            return "Error: Motion planner produced invalid output. Try rephrasing the action."
