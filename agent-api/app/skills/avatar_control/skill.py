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
    version = "3.0.0"
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
                                    "Joint name → {x, y, z}. "
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
        # Merge onto rest pose so the frontend always receives a full skeleton.
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
