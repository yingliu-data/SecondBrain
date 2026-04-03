import json
import logging
from app.skills.base import BaseSkill
from app.skills.avatar_control.poses import POSES, POSE_NAMES, MOVEMENT_CYCLES, CYCLE_NAMES
from app.skills.avatar_control.motion import build_animation, EasingType

logger = logging.getLogger("skills.avatar_control")

VALID_JOINTS = {
    "hipCentre", "neck",
    "leftHip", "leftKnee", "leftAnkle", "leftToe",
    "rightHip", "rightKnee", "rightAnkle", "rightToe",
    "leftShoulder", "leftElbow", "leftWrist",
    "rightShoulder", "rightElbow", "rightWrist",
}


class AvatarControlSkill(BaseSkill):
    name = "avatar_control"
    display_name = "Avatar Control"
    description = "Control a 3D avatar's body pose and animations."
    version = "2.0.0"
    execution_side = "server"
    keywords = [
        "avatar", "robot", "control", "move", "pose", "arm", "leg",
        "hand", "wave", "dance", "raise", "point", "gesture",
        "walk", "step", "clap", "nod", "bow", "shrug", "reach",
    ]

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "set_pose",
                    "description": (
                        "Set the avatar to a predefined full-body pose. "
                        "Use this for common poses like waving, pointing, T-pose, etc."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pose_name": {
                                "type": "string",
                                "enum": POSE_NAMES,
                                "description": "Name of the predefined pose.",
                            },
                            "duration_ms": {
                                "type": "integer",
                                "description": "Transition duration in milliseconds.",
                                "default": 500,
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
                        "Move specific joints to target 3D positions. "
                        "Use when the user wants to move individual body parts. "
                        "Coordinates are in Y-up convention: x=left/right, y=up/down, z=forward/back."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "joints": {
                                "type": "object",
                                "description": (
                                    "Dict of joint names to {x, y, z} target positions. "
                                    "Valid joints: " + ", ".join(sorted(VALID_JOINTS))
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
                            "duration_ms": {
                                "type": "integer",
                                "description": "Transition duration in milliseconds.",
                                "default": 500,
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
                        "Play a sequence of predefined poses as an animation. "
                        "Use for dancing, repeated gestures, or multi-step movements."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sequence": {
                                "type": "array",
                                "description": "Array of pose steps to play in order.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "pose_name": {
                                            "type": "string",
                                            "enum": POSE_NAMES,
                                        },
                                        "hold_ms": {
                                            "type": "integer",
                                            "description": "How long to hold this pose before transitioning.",
                                            "default": 500,
                                        },
                                    },
                                    "required": ["pose_name"],
                                },
                            },
                            "loop": {
                                "type": "boolean",
                                "description": "Whether to loop the animation.",
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
                        "Plan a smooth multi-step movement with interpolation. "
                        "ALWAYS use this for walking, clapping, nodding, bowing, "
                        "waving, or shrugging."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": CYCLE_NAMES,
                                "description": "Movement to perform.",
                            },
                            "repeats": {
                                "type": "integer",
                                "description": "How many times to repeat.",
                                "default": 1,
                            },
                            "speed": {
                                "type": "string",
                                "enum": ["slow", "normal", "fast"],
                                "description": "Movement speed.",
                                "default": "normal",
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
            return self._plan_movement(arguments)
        return f"Error: Unknown tool '{tool_name}'"

    def _set_pose(self, args: dict) -> str:
        pose_name = args.get("pose_name", "")
        if pose_name not in POSES:
            return f"Error: Unknown pose '{pose_name}'. Available: {', '.join(POSE_NAMES)}"
        duration_ms = args.get("duration_ms", 500)
        return json.dumps({
            "type": "pose",
            "joints": POSES[pose_name],
            "duration_ms": duration_ms,
        })

    def _move_joints(self, args: dict) -> str:
        joints = args.get("joints", {})
        invalid = set(joints.keys()) - VALID_JOINTS
        if invalid:
            return f"Error: Invalid joints: {', '.join(invalid)}. Valid: {', '.join(sorted(VALID_JOINTS))}"
        duration_ms = args.get("duration_ms", 500)
        return json.dumps({
            "type": "pose",
            "joints": joints,
            "duration_ms": duration_ms,
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
                "hold_ms": step.get("hold_ms", 500),
            })
        return json.dumps({
            "type": "animation",
            "frames": frames,
            "loop": args.get("loop", False),
        })

    # ── Speed name → duration per segment (ms) ──
    _SPEED_MAP = {"slow": 800, "normal": 500, "fast": 250}

    def _plan_movement(self, args: dict) -> str:
        action = args.get("action", "")
        repeats = max(1, min(args.get("repeats", 1), 10))
        speed = args.get("speed", "normal")
        duration_ms = self._SPEED_MAP.get(speed, 500)

        cycle_keys = MOVEMENT_CYCLES.get(action)
        if not cycle_keys:
            return f"Error: Unknown action '{action}'. Available: {', '.join(CYCLE_NAMES)}"

        keyframes = [POSES[k] for k in cycle_keys]

        single_cycle = build_animation(
            keyframes,
            default_easing=EasingType.EASE_IN_OUT,
            default_duration_ms=duration_ms,
        )

        frames = single_cycle * repeats

        return json.dumps({
            "type": "animation",
            "frames": frames[:200],
            "loop": False,
        })
