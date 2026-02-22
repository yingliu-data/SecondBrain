import asyncio
import logging
import shlex

from app.skills.base import BaseSkill

logger = logging.getLogger("skills.gitlab_cli")

ALLOWED_SUBCOMMANDS = {
    "mr", "issue", "pipeline", "ci", "repo", "release",
    "variable", "label", "user", "ssh-key", "duo",
    "config", "alias", "auth", "job",
}

MAX_OUTPUT = 4000  # truncate long output to keep LLM context manageable


class GitLabCLISkill(BaseSkill):
    name = "gitlab_cli"
    display_name = "GitLab CLI"
    description = (
        "Run GitLab CLI (glab) commands on the server. "
        "Manage merge requests, issues, pipelines, repos, releases, "
        "CI/CD variables, and more — all from the terminal."
    )
    version = "1.0.0"
    execution_side = "server"

    keywords = [
        "gitlab", "glab", "merge request", "MR", "pipeline",
        "CI/CD", "ci", "cd", "issue", "repo", "release",
        "variable", "label", "ssh-key", "duo",
    ]

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "glab_run",
                    "description": (
                        "Run a glab (GitLab CLI) command on the server and return its output. "
                        "Pass the subcommand and arguments — do NOT include the leading 'glab'. "
                        "Examples: 'mr list --assignee=@me', 'issue create --title \"Bug\" --label bug', "
                        "'pipeline ci view', 'repo view --web'. "
                        "Allowed subcommands: mr, issue, pipeline, ci, repo, release, variable, "
                        "label, user, ssh-key, duo, config, alias, auth, job."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "args": {
                                "type": "string",
                                "description": (
                                    "The glab subcommand and arguments (without the leading 'glab'). "
                                    "Example: 'mr list --state=merged' or 'issue view 15'."
                                ),
                            },
                        },
                        "required": ["args"],
                    },
                },
            }
        ]

    async def execute(self, tool_name: str, arguments: dict) -> str:
        args_str = arguments.get("args", "").strip()
        if not args_str:
            return "Error: No glab arguments provided."

        try:
            parts = shlex.split(args_str)
        except ValueError as e:
            return f"Error: Invalid arguments — {e}"

        subcommand = parts[0] if parts else ""
        if subcommand not in ALLOWED_SUBCOMMANDS:
            return (
                f"Error: '{subcommand}' is not an allowed glab subcommand. "
                f"Allowed: {', '.join(sorted(ALLOWED_SUBCOMMANDS))}"
            )

        cmd = ["glab"] + parts
        logger.info(f"Running: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            return "Error: glab command timed out after 30s."
        except FileNotFoundError:
            return (
                "Error: glab is not installed on the server. "
                "Install it with: brew install glab (macOS) or see https://gitlab.com/gitlab-org/cli"
            )
        except Exception as e:
            return f"Error: Failed to run glab — {type(e).__name__}: {e}"

        out = stdout.decode().strip()
        err = stderr.decode().strip()

        if proc.returncode != 0:
            msg = err or out or "Unknown error"
            return f"Error (exit {proc.returncode}): {msg[:MAX_OUTPUT]}"

        result = out or "(no output)"
        if len(result) > MAX_OUTPUT:
            result = result[:MAX_OUTPUT] + f"\n... (truncated, {len(out)} chars total)"
        return result
