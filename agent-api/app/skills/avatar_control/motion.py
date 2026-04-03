"""Server-side motion interpolation engine.

Takes keyframe poses (joint XYZ coordinates) and generates smooth
intermediate frames with easing/velocity control. Output is a list of
animation frames ready for the frontend's setTimeout playback chain.
"""

from enum import Enum
from app.skills.avatar_control.body import clamp_joint

MAX_FRAMES = 200


class EasingType(str, Enum):
    LINEAR = "linear"
    EASE_IN = "ease_in"
    EASE_OUT = "ease_out"
    EASE_IN_OUT = "ease_in_out"


def ease(t: float, easing: EasingType) -> float:
    """Map normalized time t in [0,1] to an eased value in [0,1]."""
    t = max(0.0, min(1.0, t))
    if easing == EasingType.LINEAR:
        return t
    if easing == EasingType.EASE_IN:
        return t * t
    if easing == EasingType.EASE_OUT:
        return 1.0 - (1.0 - t) ** 2
    # EASE_IN_OUT: smoothstep
    return 3.0 * t * t - 2.0 * t * t * t


def lerp_joints(
    start: dict[str, dict[str, float]],
    end: dict[str, dict[str, float]],
    t: float,
) -> dict[str, dict[str, float]]:
    """Linear interpolation between two full-body joint dicts at parameter t.

    Joints present in both dicts are interpolated per-axis.
    Joints in only one dict are carried through unchanged.
    """
    all_joints = set(start) | set(end)
    result: dict[str, dict[str, float]] = {}
    for joint in all_joints:
        s = start.get(joint)
        e = end.get(joint)
        if s and e:
            result[joint] = {
                "x": s["x"] + (e["x"] - s["x"]) * t,
                "y": s["y"] + (e["y"] - s["y"]) * t,
                "z": s["z"] + (e["z"] - s["z"]) * t,
            }
        elif s:
            result[joint] = dict(s)
        elif e:
            result[joint] = dict(e)
    return result


def interpolate_segment(
    start: dict[str, dict[str, float]],
    end: dict[str, dict[str, float]],
    duration_ms: int,
    easing: EasingType = EasingType.EASE_IN_OUT,
    frame_interval_ms: int = 80,
) -> list[dict]:
    """Generate interpolated frames between two poses.

    Returns a list of {"joints": {...}, "hold_ms": frame_interval_ms} dicts.
    The 80ms default (~12.5fps) works well with AvatarRenderer's SLERP
    smoothing (factor 0.3 at 60fps render rate).
    """
    if duration_ms <= 0 or frame_interval_ms <= 0:
        return [{"joints": _clamp_all(end), "hold_ms": max(frame_interval_ms, 1)}]

    num_steps = max(1, duration_ms // frame_interval_ms)
    frames: list[dict] = []
    for i in range(1, num_steps + 1):
        t = ease(i / num_steps, easing)
        joints = lerp_joints(start, end, t)
        frames.append({
            "joints": _clamp_all(joints),
            "hold_ms": frame_interval_ms,
        })
    return frames


def build_animation(
    keyframes: list[dict[str, dict[str, float]]],
    default_easing: EasingType = EasingType.EASE_IN_OUT,
    default_duration_ms: int = 500,
    frame_interval_ms: int = 80,
) -> list[dict]:
    """Chain multiple keyframes into a full interpolated frame sequence.

    Each keyframe is a full-body joint coordinate dict.
    Returns the flat list of animation frames, capped at MAX_FRAMES.
    """
    if len(keyframes) < 2:
        if keyframes:
            return [{"joints": _clamp_all(keyframes[0]), "hold_ms": default_duration_ms}]
        return []

    all_frames: list[dict] = []
    for i in range(len(keyframes) - 1):
        segment = interpolate_segment(
            keyframes[i],
            keyframes[i + 1],
            duration_ms=default_duration_ms,
            easing=default_easing,
            frame_interval_ms=frame_interval_ms,
        )
        all_frames.extend(segment)
        if len(all_frames) >= MAX_FRAMES:
            break

    return all_frames[:MAX_FRAMES]


def apply_relative_movement(
    base_pose: dict[str, dict[str, float]],
    joint_offsets: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Add relative deltas to a base pose, clamping each joint."""
    result = {k: dict(v) for k, v in base_pose.items()}
    for joint, offset in joint_offsets.items():
        if joint in result:
            result[joint] = clamp_joint(joint, {
                "x": result[joint]["x"] + offset.get("dx", 0.0),
                "y": result[joint]["y"] + offset.get("dy", 0.0),
                "z": result[joint]["z"] + offset.get("dz", 0.0),
            })
    return result


def _clamp_all(joints: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Clamp all joints in a pose to their reachable limits."""
    return {joint: clamp_joint(joint, coords) for joint, coords in joints.items()}
