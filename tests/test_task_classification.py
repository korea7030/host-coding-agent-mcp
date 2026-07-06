from host_coding_agent.task_classification import (
    classify_non_development_task,
    non_development_response,
)


def test_rejects_authentication_and_runtime_management():
    cases = {
        "구글 OAuth 토큰 갱신해줘": "authentication",
        "invest-bot에 fanding MCP를 설치해줘": "mcp_management",
        "Hermes skill을 활성화해줘": "skill_management",
        "Playwright Chromium을 설치해줘": "runtime_dependency",
    }
    for task, category in cases.items():
        result = classify_non_development_task(task)
        assert result is not None
        assert result.category == category


def test_allows_project_development_and_dependency_changes():
    for task in (
        "src/index.ts에서 Playwright 경로 처리 코드를 수정해줘",
        "package.json에 playwright 의존성을 추가해줘",
        "requirements.txt를 업데이트하고 테스트해줘",
        "fanding MCP의 refresh_session 버그를 수정해줘",
    ):
        assert classify_non_development_task(task) is None


def test_rejection_is_structured_and_non_retryable():
    result = non_development_response("MCP server를 등록해줘")

    assert result is not None
    assert result["error_code"] == "non_development_task"
    assert result["retryable"] is False
