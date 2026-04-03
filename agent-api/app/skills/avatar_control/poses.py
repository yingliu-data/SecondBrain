"""Predefined pose coordinate library for the 3D avatar.

All coordinates are in Three.js convention (Y-up, right-handed).
Joint names match the HIERARCHY in kinetic.py:
  hipCentre, neck, leftHip, leftKnee, leftAnkle, leftToe,
  rightHip, rightKnee, rightAnkle, rightToe,
  leftShoulder, leftElbow, leftWrist,
  rightShoulder, rightElbow, rightWrist
"""

# Base standing pose — arms at sides, feet shoulder-width apart
_STANDING_BASE = {
    "hipCentre":     {"x": 0.0,   "y": 0.9,  "z": 0.0},
    "neck":          {"x": 0.0,   "y": 1.45, "z": 0.0},
    "leftHip":       {"x": -0.1,  "y": 0.85, "z": 0.0},
    "rightHip":      {"x": 0.1,   "y": 0.85, "z": 0.0},
    "leftKnee":      {"x": -0.1,  "y": 0.48, "z": 0.0},
    "rightKnee":     {"x": 0.1,   "y": 0.48, "z": 0.0},
    "leftAnkle":     {"x": -0.1,  "y": 0.05, "z": 0.0},
    "rightAnkle":    {"x": 0.1,   "y": 0.05, "z": 0.0},
    "leftToe":       {"x": -0.1,  "y": 0.0,  "z": 0.08},
    "rightToe":      {"x": 0.1,   "y": 0.0,  "z": 0.08},
    "leftShoulder":  {"x": -0.18, "y": 1.4,  "z": 0.0},
    "rightShoulder": {"x": 0.18,  "y": 1.4,  "z": 0.0},
}


def _with_base(**overrides):
    """Return a copy of standing base with specific joint overrides."""
    pose = {k: dict(v) for k, v in _STANDING_BASE.items()}
    for joint, coords in overrides.items():
        pose[joint] = dict(coords)
    return pose


POSES: dict[str, dict] = {
    "t_pose": {
        **{k: dict(v) for k, v in _STANDING_BASE.items()},
        "leftElbow":     {"x": -0.45, "y": 1.4,  "z": 0.0},
        "leftWrist":     {"x": -0.7,  "y": 1.4,  "z": 0.0},
        "rightElbow":    {"x": 0.45,  "y": 1.4,  "z": 0.0},
        "rightWrist":    {"x": 0.7,   "y": 1.4,  "z": 0.0},
    },

    "rest": _with_base(
        leftElbow={"x": -0.2, "y": 1.15, "z": 0.05},
        leftWrist={"x": -0.18, "y": 0.95, "z": 0.08},
        rightElbow={"x": 0.2, "y": 1.15, "z": 0.05},
        rightWrist={"x": 0.18, "y": 0.95, "z": 0.08},
    ),

    "wave_right": _with_base(
        leftElbow={"x": -0.2, "y": 1.15, "z": 0.05},
        leftWrist={"x": -0.18, "y": 0.95, "z": 0.08},
        rightElbow={"x": 0.35, "y": 1.55, "z": 0.0},
        rightWrist={"x": 0.45, "y": 1.75, "z": 0.05},
    ),

    "wave_left": _with_base(
        leftElbow={"x": -0.35, "y": 1.55, "z": 0.0},
        leftWrist={"x": -0.45, "y": 1.75, "z": 0.05},
        rightElbow={"x": 0.2, "y": 1.15, "z": 0.05},
        rightWrist={"x": 0.18, "y": 0.95, "z": 0.08},
    ),

    "hands_up": _with_base(
        leftElbow={"x": -0.22, "y": 1.6, "z": 0.0},
        leftWrist={"x": -0.22, "y": 1.85, "z": 0.0},
        rightElbow={"x": 0.22, "y": 1.6, "z": 0.0},
        rightWrist={"x": 0.22, "y": 1.85, "z": 0.0},
    ),

    "point_right": _with_base(
        leftElbow={"x": -0.2, "y": 1.15, "z": 0.05},
        leftWrist={"x": -0.18, "y": 0.95, "z": 0.08},
        rightElbow={"x": 0.4, "y": 1.4, "z": 0.0},
        rightWrist={"x": 0.7, "y": 1.4, "z": 0.0},
    ),

    "point_left": _with_base(
        leftElbow={"x": -0.4, "y": 1.4, "z": 0.0},
        leftWrist={"x": -0.7, "y": 1.4, "z": 0.0},
        rightElbow={"x": 0.2, "y": 1.15, "z": 0.05},
        rightWrist={"x": 0.18, "y": 0.95, "z": 0.08},
    ),

    "dab": _with_base(
        leftElbow={"x": -0.4, "y": 1.55, "z": 0.0},
        leftWrist={"x": -0.55, "y": 1.7, "z": -0.05},
        rightElbow={"x": 0.35, "y": 1.3, "z": 0.1},
        rightWrist={"x": 0.15, "y": 1.5, "z": 0.05},
    ),

    "superhero": _with_base(
        leftElbow={"x": -0.3, "y": 1.2, "z": 0.0},
        leftWrist={"x": -0.35, "y": 1.0, "z": 0.0},
        rightElbow={"x": 0.3, "y": 1.2, "z": 0.0},
        rightWrist={"x": 0.35, "y": 1.0, "z": 0.0},
        leftKnee={"x": -0.15, "y": 0.48, "z": 0.0},
        rightKnee={"x": 0.15, "y": 0.48, "z": 0.0},
        leftAnkle={"x": -0.18, "y": 0.05, "z": 0.0},
        rightAnkle={"x": 0.18, "y": 0.05, "z": 0.0},
    ),
}

POSE_NAMES = list(POSES.keys())
