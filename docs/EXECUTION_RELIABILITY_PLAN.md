# Execution reliability implementation plan

## 1. 목적

이 문서는 Hermes agent가 host-coding-agent MCP를 실제 개발 경로로 사용할 때 반복해서
나오는 실행 혼동과 실패를 줄이기 위한 구현 계획이다.

핵심 문제는 "agent CLI가 설치되어 있음"과 "현재 profile/cwd에서 안전하게 실행 가능함"이
서로 다른 상태라는 점이다. 따라서 CLI availability, profile runtime, path mapping,
sandbox, isolation/delivery 조합, proposal 상태를 분리해서 진단하고 응답해야 한다.

## 2. 현재 확인된 문제

| 영역 | 증상 | 원인/의미 |
|---|---|---|
| Runtime registration | `Docker runtime is not registered for this profile` | profile의 Docker container mount 정보가 MCP에 등록되지 않음 |
| Sandbox | `sandbox-exec: sandbox_apply: Operation not permitted` | macOS sandbox-exec 적용 자체 실패. agent CLI 실패와 분리해야 함 |
| API 조합 | `delivery_mode applies only to worktree isolation` | direct mode와 delivery mode 조합이 API 초기에 명확히 드러나지 않음 |
| Direct safety | `applied_immediately: true` | direct는 원본 workspace를 즉시 수정하므로 read-only 의도와 구분 필요 |
| Path mapping | `/opt/data/...` → `/Users/...` | 정상 동작이지만 사용자가 mapping 실패로 오해하기 쉬움 |
| Discovery 한계 | `check_host_coding_agents`는 CLI만 확인 | CLI available이어도 runtime/sandbox/cwd에서 실패 가능 |
| non-development | `non_development_task` 재시도 | host-coding-agent가 처리하지 않아야 하는 작업을 재시도하는 혼동 |
| Proposal 상태 | proposal 생성과 적용 완료 혼동 | `proposed`, `approved`, `applied`, `rejected` 상태를 명확히 보여줘야 함 |
| HTTP stream | `ClosedResourceError` | MCP server alive와 Hermes HTTP stream client 상태를 분리해서 봐야 함 |

## 3. 구현 원칙

1. 실행 가능성은 단계별로 진단한다.
2. agent CLI availability와 profile execution readiness를 분리한다.
3. sandbox 실패는 agent 실패로 섞지 않는다.
4. direct mode는 즉시 쓰기임을 모든 응답에서 명확히 드러낸다.
5. worktree manual proposal은 적용 완료가 아니라 승인 대기 상태임을 강제한다.
6. `non_development_task`는 최종 비재시도 결과로 유지한다.
7. 오류 메시지는 사용자가 다음에 할 수 있는 조치를 포함해야 한다.

## 4. 단계별 구현 계획

### P0-1. `check_execution_health` 도구 추가

새 MCP tool을 추가한다.

```text
check_execution_health(cwd?: string, isolation_mode?: "direct" | "worktree")
```

반환 필드:

```json
{
  "ok": false,
  "profile": "invest-bot",
  "requested_cwd": "/opt/data/profiles/invest-bot/workspace",
  "resolved_cwd": "/Users/jaehyunlee/.hermes-invest/profiles/invest-bot/workspace",
  "path_mapping_applied": true,
  "checks": {
    "auth": {"ok": true},
    "agent_cli": {"ok": true},
    "runtime_registration": {"ok": true},
    "cwd_mapping": {"ok": true},
    "allowed_roots": {"ok": true},
    "sandbox": {"ok": false, "error": "sandbox-exec ..."},
    "direct_smoke": {"ok": true},
    "worktree_available": {"ok": false, "reason": "not a git repository"}
  },
  "recommended_next_action": "sandbox-exec is unavailable; use configured bypass policy or fix host sandbox permission."
}
```

구현 위치:

- `server.py`: MCP tool 등록
- `host_coding_agent/health.py`: 진단 로직
- `tests/test_execution_health.py`: 단위 테스트

검증:

- runtime 미등록 profile이면 `runtime_registration.ok=false`
- container cwd는 host cwd로 resolve되어 `path_mapping_applied=true`
- Git이 없는 workspace에서 worktree availability가 false
- CLI availability가 true여도 전체 `ok`가 false일 수 있음

### P0-2. `check_host_coding_agents` 응답 확장

Status: implemented.

기존 tool은 유지하되 의미를 명확히 한다.

추가 필드:

```json
{
  "profile": "invest-bot",
  "discovery_scope": "cli_availability",
  "execution_health_tool": "check_execution_health",
  "warning": "CLI availability does not guarantee profile runtime, cwd mapping, or sandbox readiness."
}
```

기본 호출에서는 compact execution health도 함께 반환한다.

```json
{
  "execution_ready": false,
  "execution_health": {
    "requested_cwd": "/opt/data/profiles/invest-bot/workspace",
    "resolved_cwd": "/Users/jaehyunlee/.hermes-invest/profiles/invest-bot/workspace",
    "path_mapping_applied": true,
    "failed_checks": ["sandbox"],
    "checks": {
      "runtime_registration": {"ok": true},
      "cwd_mapping": {"ok": true},
      "allowed_roots": {"ok": true},
      "sandbox": {"ok": false}
    }
  }
}
```

목표:

- `available=true`를 "실행 가능"으로 오해하지 않게 한다.
- Hermes agent가 개발 실행 전 `check_execution_health`를 호출하도록 유도한다.

검증:

- 기존 response schema와 호환성 유지
- README/skill 문서에 discovery → health → execution 순서 반영

### P0-3. Sandbox 진단 분리

Status: implemented.

현재 `_sandbox_prefix()`는 `sandbox-exec` command를 붙이고, 실패는 실행 후 agent 실패로
보인다. 이를 별도 진단 가능하게 만든다.

구현:

- `host_coding_agent/sandbox.py` 추가
- `check_sandbox_exec(cwd, writable_paths)` 구현
- `sandbox-exec` binary 없음, `sandbox_apply` 실패, 정책 compile 실패를 구분
- `_run_attempt()` 결과에 `failure_category="sandbox"` 추가 가능 여부 검토

현재 구현은 별도 `sandbox.py` 대신 `host_coding_agent/health.py`와
`host_coding_agent/runner.py`에 나누어 적용한다.

- `check_execution_health`는 실행 전 sandbox probe를 수행한다.
- 실제 agent attempt 실패는 `AttemptResult.failure_category`와
  `AttemptResult.failure_detail`로 분리된다.
- `sandbox-exec: sandbox_apply: Operation not permitted` 또는 exit code 71은
  `sandbox_apply_failed`로 기록된다.
- 모든 attempt가 sandbox 계열 실패이면 `RunResult.error`는
  `sandbox failed for all attempted agents`가 된다.
- audit log attempt에도 `failure_category`가 포함된다.

응답 예:

```json
{
  "ok": false,
  "category": "sandbox_unavailable",
  "command": "sandbox-exec",
  "exit_code": 71,
  "stderr": "sandbox-exec: sandbox_apply: Operation not permitted",
  "bypass_available": false
}
```

검증:

- `sandbox-exec` 없음
- `sandbox-exec`가 exit 71 반환
- sandbox 성공

### P0-4. Runtime registration 오류 메시지 개선

기존:

```text
Docker runtime is not registered for this profile
```

개선:

```text
Docker runtime is not registered for profile 'invest-bot'.
The requested cwd is a container path and must be mapped through a registered Docker runtime.
Expected container roots: /opt/data/profiles/invest-bot/workspace
Registered profiles: dev-bot, research-bot, youtube-bot
Next action: ensure the Hermes development-policy plugin can call /runtime/register with MCP_HOST_CODING_AGENT_API_KEY, then restart the profile gateway.
```

구현:

- `RuntimeRegistry.resolve()`에서 진단 가능한 exception payload 또는 상세 메시지 제공
- `RuntimeRegistry.status(profile_name)` 추가

검증:

- 등록 없는 profile에서 registered profile 목록 포함
- allowed container roots 포함
- 다음 조치 문구 포함

## 5. P1 구현 계획

### P1-1. direct + delivery mode validation 개선

Status: implemented.

현재 direct mode에서 `delivery_mode != manual`이면 에러다.

유지할 정책:

| isolation_mode | delivery_mode | 결과 |
|---|---|---|
| direct | manual | valid |
| direct | auto/commit/pr/report | invalid |
| worktree | manual | valid |
| worktree | commit | valid |
| worktree | auto | valid |
| worktree | pr | valid if profile allows PR |

개선:

- 초기 validation 단계에서 명확한 structured error 반환
- error에 valid combinations 포함
- `run_development_task`와 `start_development_task` 모두 job 생성/agent 실행 전에
  같은 validation 응답을 반환한다.

응답 예:

```json
{
  "ok": false,
  "error_code": "invalid_isolation_delivery_combination",
  "error": "delivery_mode applies only to worktree isolation",
  "valid_combinations": [
    {"isolation_mode": "direct", "delivery_mode": "manual"},
    {"isolation_mode": "worktree", "delivery_mode": "manual|commit|auto|pr"}
  ]
}
```

### P1-2. Direct read-only 안전장치

Status: implemented.

문제:

- 사용자가 "확인해줘"라고 해도 direct mode는 내부적으로 `apply_patch`로 실행된다.
- agent가 파일을 변경할 수 있다.

구현 옵션:

1. `run_development_task`에 `intent` 또는 `write_policy` 추가

```text
direct_write_policy: "allow" | "fail_if_changed"
```

2. direct 실행 전후 git diff 또는 filesystem snapshot 비교

Git workspace:

- `git status --porcelain`
- `git diff --stat`

non-Git workspace:

- 파일 mtime/size/hash snapshot은 비용이 크므로 제한된 파일 수/확장자만 대상으로 시작

3. read-only 의도 감지 task는 `run_coding_agent` read_only 또는 `check_execution_health`로 유도

검증:

- `write_policy=fail_if_changed`에서 변경 발생 시 실패
- 결과에 `changed_files`, `diff_summary`, `applied_immediately` 포함

현재 구현:

- `run_development_task`, `start_development_task`, `run_antigravity`,
  `run_codex`, `run_opencode`가 `direct_write_policy`를 받는다.
- direct 실행 전후 workspace snapshot을 비교해 `changed_files`와
  `changed_file_count`를 반환한다.
- `direct_write_policy=fail_if_changed`에서 변경이 감지되면
  `ok=false`, `error_code=direct_write_policy_violation`,
  `write_policy_violated=true`를 반환한다.
- `.git`, `node_modules`, venv, cache 계열 runtime directory는 snapshot에서 제외한다.
- 이미 발생한 direct 변경을 자동 rollback하지는 않는다.

### P1-3. 공통 path mapping response 정리

모든 실행 계열 tool 응답에 다음 필드를 일관되게 포함한다.

```json
{
  "requested_cwd": "/opt/data/profiles/invest-bot/workspace",
  "resolved_cwd": "/Users/jaehyunlee/.hermes-invest/profiles/invest-bot/workspace",
  "path_mapping_applied": true,
  "path_mapping_note": "Resolved host cwd is expected when the caller passes a Docker container path."
}
```

대상:

- `run_development_task`
- `start_development_task`
- `run_coding_agent`
- `run_opencode`
- `run_codex`
- `run_antigravity`
- `check_execution_health`

### P1-4. Proposal 상태 응답 정리

manual worktree mode 응답에 문구와 상태를 강제한다.

```json
{
  "status": "proposed",
  "requires_approval": true,
  "applied": false,
  "message": "Proposal created but not applied. Use /apply_proposal <id> <sha256> to apply."
}
```

승인/적용 결과:

- `approved`: 승인됐지만 아직 적용 안 됨
- `applied`: 원본 workspace에 적용 완료
- `rejected`: 거절됨

검증:

- proposal 생성 응답에서 "applied"로 해석될 수 있는 문구 금지
- apply endpoint 성공 시에만 `applied=true`

### P1-5. `non_development_task` 안내 개선

현재 분류는 있다. 응답을 더 실용적으로 만든다.

추가 필드:

```json
{
  "suggested_route": "Hermes profile management",
  "do_not_retry_with_host_coding_agent": true,
  "examples": [
    "Use Hermes mcp install/configure for MCP lifecycle.",
    "Use target MCP auth/login for OAuth or token refresh."
  ]
}
```

검증:

- OAuth/token refresh는 retryable false
- MCP 설치는 runtime/profile route 안내
- host project dependency 수정은 여전히 개발 작업으로 허용

## 6. P2 구현 계획

### P2-1. HTTP stream reconnect 진단

MCP server health와 Hermes HTTP stream client 상태를 분리한다.

추가:

- `/healthz` HTTP endpoint
- `/readyz` HTTP endpoint
- MCP tool registration count expose

응답 예:

```json
{
  "ok": true,
  "server": "host-coding-agent",
  "tools": 21,
  "runtime_profiles": ["dev-bot", "invest-bot"]
}
```

Hermes에서 `ClosedResourceError`가 나도 `/healthz`가 true이면 server down으로 판단하지 않는다.

### P2-2. Documentation/skill 업데이트

업데이트 대상:

- `README.md`
- `docs/AGENT_ROUTING.md`
- `docs/DEVELOPMENT_ENFORCEMENT.md`
- `skills/host-coding-agent/SKILL.md`
- `hermes_plugins/development-policy/SOUL_APPEND.md`

새 표준 흐름:

```text
check_host_coding_agents
→ check_execution_health
→ run_development_task 또는 start_development_task
→ get_async_job_events
```

## 7. 권장 구현 순서

1. `check_execution_health` skeleton 추가
2. runtime registration status와 cwd mapping check 구현
3. sandbox health check 구현
4. `check_host_coding_agents`에 health 안내 추가
5. direct + delivery mode structured validation 추가
6. 공통 path mapping response 정리
7. direct read-only/write safety 구현
8. proposal 상태 응답 정리
9. non-development 안내 개선
10. `/healthz`, `/readyz` 추가
11. README/docs/skill/policy 문서 업데이트

## 8. 첫 번째 구현 PR 범위

첫 PR 또는 첫 commit은 P0까지만 포함한다.

포함:

- `check_execution_health`
- runtime status
- cwd mapping status
- sandbox status
- `check_host_coding_agents` 경고/안내
- runtime registration 오류 메시지 개선
- 테스트

제외:

- direct write snapshot/diff
- proposal 상태 UX 정리
- HTTP stream reconnect endpoint
- skill 문서 전체 개편

이렇게 끊어야 현재 실제 장애인 runtime/sandbox 진단을 빠르게 개선할 수 있고, direct safety
같은 정책 변경은 별도 검토할 수 있다.
