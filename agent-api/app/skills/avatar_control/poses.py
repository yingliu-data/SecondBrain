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

    # ── Single-arm raise poses ──

    "raise_right_hand": _with_base(
        leftElbow={"x": -0.2, "y": 1.15, "z": 0.05},
        leftWrist={"x": -0.18, "y": 0.95, "z": 0.08},
        rightElbow={"x": 0.25, "y": 1.6, "z": 0.0},
        rightWrist={"x": 0.25, "y": 1.85, "z": 0.0},
    ),

    "raise_left_hand": _with_base(
        leftElbow={"x": -0.25, "y": 1.6, "z": 0.0},
        leftWrist={"x": -0.25, "y": 1.85, "z": 0.0},
        rightElbow={"x": 0.2, "y": 1.15, "z": 0.05},
        rightWrist={"x": 0.18, "y": 0.95, "z": 0.08},
    ),

    # ── Single-leg raise poses ──

    "raise_right_leg": _with_base(
        leftElbow={"x": -0.2, "y": 1.15, "z": 0.05},
        leftWrist={"x": -0.18, "y": 0.95, "z": 0.08},
        rightElbow={"x": 0.2, "y": 1.15, "z": 0.05},
        rightWrist={"x": 0.18, "y": 0.95, "z": 0.08},
        rightKnee={"x": 0.1, "y": 0.65, "z": 0.2},
        rightAnkle={"x": 0.1, "y": 0.45, "z": 0.25},
        rightToe={"x": 0.1, "y": 0.4, "z": 0.3},
    ),

    "raise_left_leg": _with_base(
        leftElbow={"x": -0.2, "y": 1.15, "z": 0.05},
        leftWrist={"x": -0.18, "y": 0.95, "z": 0.08},
        rightElbow={"x": 0.2, "y": 1.15, "z": 0.05},
        rightWrist={"x": 0.18, "y": 0.95, "z": 0.08},
        leftKnee={"x": -0.1, "y": 0.65, "z": 0.2},
        leftAnkle={"x": -0.1, "y": 0.45, "z": 0.25},
        leftToe={"x": -0.1, "y": 0.4, "z": 0.3},
    ),

    # ── New poses for motion planning ──

    "bow": _with_base(
        neck={"x": 0.0, "y": 1.35, "z": 0.1},
        leftElbow={"x": -0.2, "y": 1.1, "z": 0.08},
        leftWrist={"x": -0.18, "y": 0.9, "z": 0.1},
        rightElbow={"x": 0.2, "y": 1.1, "z": 0.08},
        rightWrist={"x": 0.18, "y": 0.9, "z": 0.1},
    ),

    "nod_down": _with_base(
        neck={"x": 0.0, "y": 1.40, "z": 0.05},
        leftElbow={"x": -0.2, "y": 1.15, "z": 0.05},
        leftWrist={"x": -0.18, "y": 0.95, "z": 0.08},
        rightElbow={"x": 0.2, "y": 1.15, "z": 0.05},
        rightWrist={"x": 0.18, "y": 0.95, "z": 0.08},
    ),

    "shrug": _with_base(
        leftShoulder={"x": -0.18, "y": 1.48, "z": 0.0},
        rightShoulder={"x": 0.18, "y": 1.48, "z": 0.0},
        leftElbow={"x": -0.28, "y": 1.25, "z": 0.05},
        leftWrist={"x": -0.25, "y": 1.05, "z": 0.08},
        rightElbow={"x": 0.28, "y": 1.25, "z": 0.05},
        rightWrist={"x": 0.25, "y": 1.05, "z": 0.08},
    ),

    "clap_open": _with_base(
        leftElbow={"x": -0.2, "y": 1.25, "z": 0.15},
        leftWrist={"x": -0.15, "y": 1.25, "z": 0.25},
        rightElbow={"x": 0.2, "y": 1.25, "z": 0.15},
        rightWrist={"x": 0.15, "y": 1.25, "z": 0.25},
    ),

    "clap_closed": _with_base(
        leftElbow={"x": -0.15, "y": 1.25, "z": 0.15},
        leftWrist={"x": -0.02, "y": 1.25, "z": 0.25},
        rightElbow={"x": 0.15, "y": 1.25, "z": 0.15},
        rightWrist={"x": 0.02, "y": 1.25, "z": 0.25},
    ),

    "reach_forward_right": _with_base(
        leftElbow={"x": -0.2, "y": 1.15, "z": 0.05},
        leftWrist={"x": -0.18, "y": 0.95, "z": 0.08},
        rightElbow={"x": 0.15, "y": 1.35, "z": 0.2},
        rightWrist={"x": 0.15, "y": 1.35, "z": 0.45},
    ),

    "reach_forward_left": _with_base(
        leftElbow={"x": -0.15, "y": 1.35, "z": 0.2},
        leftWrist={"x": -0.15, "y": 1.35, "z": 0.45},
        rightElbow={"x": 0.2, "y": 1.15, "z": 0.05},
        rightWrist={"x": 0.18, "y": 0.95, "z": 0.08},
    ),

    # Walking keyframes (in-place walking motion)
    "step_right": _with_base(
        # Right leg forward, left leg back
        rightKnee={"x": 0.1, "y": 0.5, "z": 0.12},
        rightAnkle={"x": 0.1, "y": 0.05, "z": 0.12},
        rightToe={"x": 0.1, "y": 0.0, "z": 0.18},
        leftKnee={"x": -0.1, "y": 0.5, "z": -0.05},
        leftAnkle={"x": -0.1, "y": 0.05, "z": -0.05},
        leftToe={"x": -0.1, "y": 0.0, "z": 0.02},
        # Counter-swing arms
        leftElbow={"x": -0.18, "y": 1.15, "z": 0.1},
        leftWrist={"x": -0.16, "y": 0.95, "z": 0.15},
        rightElbow={"x": 0.18, "y": 1.15, "z": -0.05},
        rightWrist={"x": 0.16, "y": 0.95, "z": -0.02},
    ),

    "step_left": _with_base(
        # Left leg forward, right leg back
        leftKnee={"x": -0.1, "y": 0.5, "z": 0.12},
        leftAnkle={"x": -0.1, "y": 0.05, "z": 0.12},
        leftToe={"x": -0.1, "y": 0.0, "z": 0.18},
        rightKnee={"x": 0.1, "y": 0.5, "z": -0.05},
        rightAnkle={"x": 0.1, "y": 0.05, "z": -0.05},
        rightToe={"x": 0.1, "y": 0.0, "z": 0.02},
        # Counter-swing arms (opposite of step_right)
        rightElbow={"x": 0.18, "y": 1.15, "z": 0.1},
        rightWrist={"x": 0.16, "y": 0.95, "z": 0.15},
        leftElbow={"x": -0.18, "y": 1.15, "z": -0.05},
        leftWrist={"x": -0.16, "y": 0.95, "z": -0.02},
    ),

    "step_passing": _with_base(
        leftElbow={"x": -0.2, "y": 1.15, "z": 0.05},
        leftWrist={"x": -0.18, "y": 0.95, "z": 0.08},
        rightElbow={"x": 0.2, "y": 1.15, "z": 0.05},
        rightWrist={"x": 0.18, "y": 0.95, "z": 0.08},
    ),
}

POSE_NAMES = list(POSES.keys())

# ── Movement cycles for plan_movement tool ──
# Each cycle is a list of pose keys that form a repeatable motion loop.

MOVEMENT_CYCLES: dict[str, list[str]] = {
    "walk_cycle":  ["step_right", "step_passing", "step_left", "step_passing"],
    "wave_cycle":  ["wave_right", "rest"],
    "nod_cycle":   ["nod_down", "rest"],
    "clap_cycle":  ["clap_open", "clap_closed"],
    "bow_cycle":   ["bow", "rest"],
    "shrug_cycle": ["shrug", "rest"],
}

CYCLE_NAMES = list(MOVEMENT_CYCLES.keys())
