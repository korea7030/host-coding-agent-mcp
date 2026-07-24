from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NonDevelopmentTask:
    category: str
    suggested_route: str
    task_owner: str
    recommended_next_action: str
    examples: tuple[str, ...]


_AUTHENTICATION = re.compile(
    r"(?:oauth|로그인|인증|토큰\s*(?:갱신|재발급|refresh)|"
    r"refresh\s+(?:the\s+)?token|reauth(?:enticate)?|sign[\s-]?in)",
    re.IGNORECASE,
)
_SKILL_MANAGEMENT = re.compile(
    r"(?:(?:skill|스킬)\s*(?:을|를)?\s*(?:설치|등록|활성화|비활성화|삭제)|"
    r"(?:install|enable|disable|register|remove)\s+(?:a\s+|the\s+)?skill)",
    re.IGNORECASE,
)
_MCP_MANAGEMENT = re.compile(
    r"(?:(?:mcp|mcp\s*server)\s*(?:을|를)?\s*(?:설치|등록|연결|활성화|비활성화|삭제)|"
    r"(?:install|enable|disable|register|connect|remove)\s+"
    r"(?:a\s+|the\s+)?(?:[\w.-]+\s+)?mcp(?:\s+server)?)",
    re.IGNORECASE,
)
_RUNTIME_BROWSER_INSTALL = re.compile(
    r"(?:(?:playwright|chromium|chrome|browser|브라우저)\s*(?:을|를)?\s*"
    r"(?:설치|다운로드)|(?:install|download)\s+"
    r"(?:playwright|chromium|chrome|browser))",
    re.IGNORECASE,
)


def classify_non_development_task(task: str) -> NonDevelopmentTask | None:
    """Return the runtime owner for requests that must not reach coding agents."""

    if _AUTHENTICATION.search(task):
        return NonDevelopmentTask(
            category="authentication",
            suggested_route="Use the target MCP or skill authentication flow.",
            task_owner="target_mcp_or_profile_runtime",
            recommended_next_action=(
                "Run the OAuth/login/token refresh flow in the target MCP, "
                "Hermes profile runtime, or connected service UI."
            ),
            examples=(
                "Use the Google/Fanding MCP auth command or connection flow.",
                "Refresh credentials in the Hermes profile runtime, not through a coding agent.",
            ),
        )
    if _SKILL_MANAGEMENT.search(task):
        return NonDevelopmentTask(
            category="skill_management",
            suggested_route="Use the Hermes profile skill manager.",
            task_owner="hermes_profile_management",
            recommended_next_action=(
                "Install, enable, disable, or remove skills through the Hermes "
                "profile skill management flow."
            ),
            examples=(
                "Use the Hermes skill install/configuration command for this profile.",
                "Do not install profile skills inside the project workspace.",
            ),
        )
    if _MCP_MANAGEMENT.search(task):
        return NonDevelopmentTask(
            category="mcp_management",
            suggested_route="Use the Hermes profile MCP configuration manager.",
            task_owner="hermes_profile_management",
            recommended_next_action=(
                "Register, connect, enable, disable, or remove MCP servers through "
                "Hermes profile configuration."
            ),
            examples=(
                "Update the Hermes profile MCP server configuration.",
                "Restart or reload the Hermes profile runtime after MCP configuration changes.",
            ),
        )
    if _RUNTIME_BROWSER_INSTALL.search(task):
        return NonDevelopmentTask(
            category="runtime_dependency",
            suggested_route=(
                "Install the browser in the runtime where the target MCP executes."
            ),
            task_owner="profile_runtime_operations",
            recommended_next_action=(
                "Install runtime dependencies in the container or host runtime that "
                "executes the target MCP."
            ),
            examples=(
                "Install Playwright browsers in the MCP runtime container.",
                "Do not treat runtime browser installation as a source-code patch.",
            ),
        )
    return None


def non_development_response(task: str) -> dict[str, object] | None:
    classification = classify_non_development_task(task)
    if classification is None:
        return None
    return {
        "ok": False,
        "error_code": "non_development_task",
        "category": classification.category,
        "error": (
            "This request is runtime or profile management, not host code "
            "development. The host-coding-agent MCP did not execute it."
        ),
        "suggested_route": classification.suggested_route,
        "task_owner": classification.task_owner,
        "recommended_next_action": classification.recommended_next_action,
        "do_not_retry_with_host_coding_agent": True,
        "examples": list(classification.examples),
        "retryable": False,
    }
