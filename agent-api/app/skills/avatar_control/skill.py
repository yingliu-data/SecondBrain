import json
import logging
from app.skills.base import BaseSkill
from app.skills.avatar_control.poses import POSES, POSE_NAMES

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
    version = "1.0.0"
    execution_side = "server"
    keywords = [
        "avatar", "robot", "control", "move", "pose", "arm", "leg",
        "hand", "wave", "dance", "raise", "point", "gesture",
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
        ]

    async def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "set_pose":
            return self._set_pose(arguments)
        elif tool_name == "move_joints":
            return self._move_joints(arguments)
        elif tool_name == "animate_sequence":
            return self._animate_sequence(arguments)
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
