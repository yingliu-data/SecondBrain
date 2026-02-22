import asyncio
import logging
import shlex

from app.skills.base import BaseSkill

logger = logging.getLogger("skills.github_cli")

ALLOWED_SUBCOMMANDS = {
    "pr", "issue", "repo", "release", "run", "workflow",
    "gist", "ssh-key", "gpg-key", "secret", "variable",
    "label", "project", "status", "search", "api",
    "auth", "config", "alias", "extension", "codespace",
    "cache", "ruleset", "attestation",
}

MAX_OUTPUT = 4000  # truncate long output to keep LLM context manageable


class GitHubCLISkill(BaseSkill):
    name = "github_cli"
    display_name = "GitHub CLI"
    description = (
        "Run GitHub CLI (gh) commands on the server. "
        "Manage pull requests, issues, repos, releases, Actions workflows, "
        "gists, secrets, and more — all from the terminal."
    )
    version = "1.0.0"
    execution_side = "server"

    keywords = [
        "github", "gh", "pull request", "PR", "actions",
        "workflow", "gist", "issue", "repo", "release",
        "secret", "variable", "codespace",
    ]

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "gh_run",
                    "description": (
                        "Run a gh (GitHub CLI) command on the server and return its output. "
                        "Pass the subcommand and arguments — do NOT include the leading 'gh'. "
                        "Examples: 'pr list --assignee=@me', 'issue create --title \"Bug\" --label bug', "
                        "'run list', 'repo view', 'release list'. "
                        "Allowed subcommands: pr, issue, repo, release, run, workflow, gist, "
                        "ssh-key, gpg-key, secret, variable, label, project, status, search, "
                        "api, auth, config, alias, extension, codespace, cache, ruleset, attestation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "args": {
                                "type": "string",
                                "description": (
                                    "The gh subcommand and arguments (without the leading 'gh'). "
                                    "Example: 'pr list --state=merged' or 'issue view 15'."
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
            return "Error: No gh arguments provided."

        try:
            parts = shlex.split(args_str)
        except ValueError as e:
            return f"Error: Invalid arguments — {e}"

        subcommand = parts[0] if parts else ""
        if subcommand not in ALLOWED_SUBCOMMANDS:
            return (
                f"Error: '{subcommand}' is not an allowed gh subcommand. "
                f"Allowed: {', '.join(sorted(ALLOWED_SUBCOMMANDS))}"
            )

        cmd = ["gh"] + parts
        logger.info(f"Running: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            return "Error: gh command timed out after 30s."
        except FileNotFoundError:
            return (
                "Error: gh is not installed on the server. "
                "Install it with: brew install gh (macOS) or see https://cli.github.com"
            )
        except Exception as e:
            return f"Error: Failed to run gh — {type(e).__name__}: {e}"

        out = stdout.decode().strip()
        err = stderr.decode().strip()

        if proc.returncode != 0:
            msg = err or out or "Unknown error"
            return f"Error (exit {proc.returncode}): {msg[:MAX_OUTPUT]}"

        result = out or "(no output)"
        if len(result) > MAX_OUTPUT:
            result = result[:MAX_OUTPUT] + f"\n... (truncated, {len(out)} chars total)"
        return result
