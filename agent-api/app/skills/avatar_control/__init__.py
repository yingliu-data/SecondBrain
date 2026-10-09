"""avatar_control — the avatar skill package.

AVATAR_TOOLS: the tools whose result the client also gets as an SSE
`avatar_command{name, result}` (app/host/stream.py), and whose result the
guest gets compacted (plan_movement: a frame count, app/host/sides.py).
"""

AVATAR_TOOLS = frozenset({"set_pose", "move_joints", "animate_sequence", "plan_movement"})
