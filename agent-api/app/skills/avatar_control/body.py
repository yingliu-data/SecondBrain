"""Body model with spatial constraints, joint limits, and semantic regions.

Provides helpers for clamping joints to reachable workspace, resolving
named body regions to coordinates, and computing relative movements.
All coordinates use the same Y-up convention as poses.py.
"""

# Per-joint reachable workspace (min, max) per axis.
# Derived from the range of values across all predefined poses plus a small margin.
JOINT_LIMITS: dict[str, dict[str, tuple[float, float]]] = {
    "hipCentre":     {"x": (-0.05, 0.05), "y": (0.8, 1.0),  "z": (-0.1, 0.15)},
    "neck":          {"x": (-0.05, 0.05), "y": (1.3, 1.5),   "z": (-0.05, 0.15)},
    "leftHip":       {"x": (-0.2, -0.05), "y": (0.8, 0.9),   "z": (-0.1, 0.1)},
    "rightHip":      {"x": (0.05, 0.2),   "y": (0.8, 0.9),   "z": (-0.1, 0.1)},
    "leftKnee":      {"x": (-0.2, -0.05), "y": (0.35, 0.55), "z": (-0.1, 0.2)},
    "rightKnee":     {"x": (0.05, 0.2),   "y": (0.35, 0.55), "z": (-0.1, 0.2)},
    "leftAnkle":     {"x": (-0.25, -0.05),"y": (0.0, 0.15),  "z": (-0.15, 0.2)},
    "rightAnkle":    {"x": (0.05, 0.25),  "y": (0.0, 0.15),  "z": (-0.15, 0.2)},
    "leftToe":       {"x": (-0.2, -0.05), "y": (0.0, 0.05),  "z": (-0.05, 0.2)},
    "rightToe":      {"x": (0.05, 0.2),   "y": (0.0, 0.05),  "z": (-0.05, 0.2)},
    "leftShoulder":  {"x": (-0.25, -0.1), "y": (1.35, 1.5),  "z": (-0.05, 0.05)},
    "rightShoulder": {"x": (0.1, 0.25),   "y": (1.35, 1.5),  "z": (-0.05, 0.05)},
    "leftElbow":     {"x": (-0.55, -0.1), "y": (0.9, 1.65),  "z": (-0.1, 0.25)},
    "rightElbow":    {"x": (0.1, 0.55),   "y": (0.9, 1.65),  "z": (-0.1, 0.25)},
    "leftWrist":     {"x": (-0.75, -0.02),"y": (0.8, 1.9),   "z": (-0.15, 0.5)},
    "rightWrist":    {"x": (0.02, 0.75),  "y": (0.8, 1.9),   "z": (-0.15, 0.5)},
}

# Named spatial regions the LLM can reference for targeting.
BODY_REGIONS: dict[str, dict[str, float]] = {
    "above_head":        {"x": 0.0,  "y": 1.85, "z": 0.0},
    "head_level":        {"x": 0.0,  "y": 1.55, "z": 0.0},
    "in_front_of_chest": {"x": 0.0,  "y": 1.25, "z": 0.3},
    "chest_level":       {"x": 0.0,  "y": 1.25, "z": 0.0},
    "waist_level":       {"x": 0.0,  "y": 0.95, "z": 0.0},
    "in_front_of_waist": {"x": 0.0,  "y": 0.95, "z": 0.3},
    "left_side":         {"x": -0.5, "y": 1.2,  "z": 0.0},
    "right_side":        {"x": 0.5,  "y": 1.2,  "z": 0.0},
    "left_hip_level":    {"x": -0.2, "y": 0.9,  "z": 0.0},
    "right_hip_level":   {"x": 0.2,  "y": 0.9,  "z": 0.0},
    "forward":           {"x": 0.0,  "y": 1.2,  "z": 0.4},
    "behind":            {"x": 0.0,  "y": 1.2,  "z": -0.15},
}

REGION_NAMES = list(BODY_REGIONS.keys())


def clamp_joint(joint_name: str, coords: dict[str, float]) -> dict[str, float]:
    """Clamp a joint's XYZ coordinates to its reachable workspace."""
    limits = JOINT_LIMITS.get(joint_name)
    if not limits:
        return dict(coords)
    return {
        "x": max(limits["x"][0], min(limits["x"][1], coords.get("x", 0.0))),
        "y": max(limits["y"][0], min(limits["y"][1], coords.get("y", 0.0))),
        "z": max(limits["z"][0], min(limits["z"][1], coords.get("z", 0.0))),
    }


def resolve_region(region_name: str) -> dict[str, float] | None:
    """Look up a named body region. Returns None if not found."""
    return BODY_REGIONS.get(region_name)


def offset_from_current(
    joint_name: str,
    current: dict[str, float],
    dx: float = 0.0,
    dy: float = 0.0,
    dz: float = 0.0,
) -> dict[str, float]:
    """Compute a new position by adding a relative offset, then clamping."""
    new_coords = {
        "x": current.get("x", 0.0) + dx,
        "y": current.get("y", 0.0) + dy,
        "z": current.get("z", 0.0) + dz,
    }
    return clamp_joint(joint_name, new_coords)
