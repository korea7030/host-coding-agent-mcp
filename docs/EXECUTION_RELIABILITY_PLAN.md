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

Status: implemented.

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

현재 구현:

- legacy `run_coding_agent` 계열 응답은 `requested_cwd`, `resolved_cwd`, `cwd`,
  `worktree_cwd`, `path_mapping_applied`, `path_mapping_note`를 포함한다.
- direct mode 응답은 `requested_cwd`와 `resolved_cwd`를 분리하고 `cwd`는 실제 resolved
  host workspace를 가리킨다.
- worktree mode 응답은 `requested_cwd`, `resolved_cwd`와 별도로 실제 agent 실행 위치인
  `worktree_cwd`를 반환한다. 이 경우 `cwd`는 `worktree_cwd`와 같다.
- Docker container path가 host path로 변환되면 `path_mapping_applied=true`와
  explanatory note가 함께 반환된다.

### P1-4. Proposal 상태 응답 정리 — implemented

manual worktree mode 응답에 문구와 상태를 강제한다.

```json
{
  "status": "proposed",
  "proposal_status": "proposed",
  "approval_status": "pending",
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

구현:

- `run_development_task` manual 응답은 `proposal_status=proposed`,
  `approval_status=pending`, `requires_approval=true`, `applied=false`를 반환한다.
- `/approval/telegram` show/reject/approve 응답은 proposal 상태 payload를 함께
  반환한다.
- `ManualDelivery.deliver` 성공/재시도 결과는 `proposal_status=applied`,
  `applied=true`를 반환한다.
- `deliver_development_job`은 manual approval 대기와 delivered 상태를 구분한다.

### P1-5. `non_development_task` 안내 개선 — implemented

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

구현:

- `non_development_response`는 `do_not_retry_with_host_coding_agent=true`를 반환한다.
- category별 `task_owner`, `recommended_next_action`, `examples`를 반환한다.
- Hermes/Telegram agent는 이 응답을 최종 실패로 처리하고 같은 요청을
  `run_coding_agent`, `run_development_task`, `run_opencode` 등으로 재시도하지
  않아야 한다.

## 6. P2 구현 계획

### P2-1. HTTP stream reconnect 진단 — implemented

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

구현:

- `GET /healthz`: 서버 생존, tool count, configured profile, registered runtime profile 반환
- `GET /readyz`: `/healthz` 정보에 auth/artifact/worktree state path를 추가해 readiness 반환
- 두 endpoint는 MCP SSE stream 없이 일반 HTTP로 호출 가능하다.
- Hermes에서 MCP 호출이 `ClosedResourceError`로 실패해도 `/healthz.ok=true`이면
  서버 다운이 아니라 HTTP stream client/reconnect 문제로 분리해서 진단한다.

### P2-2. Documentation/skill 업데이트 — implemented

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

구현:

- `README.md`: discovery → health → explicit agent selection → async job/events 표준 흐름 반영
- `docs/AGENT_ROUTING.md`: agent routing 전에 `check_execution_health`를 필수 preflight로 명시
- `docs/DEVELOPMENT_ENFORCEMENT.md`: Hermes enforcement 허용 경로와 fail-closed 규칙 업데이트
- `skills/host-coding-agent/SKILL.md`: skill의 required routing sequence를 async 표준 흐름으로 업데이트
- `hermes_plugins/development-policy/SOUL_APPEND.md`: Hermes profile policy 문구를 명시 agent 선택과 health preflight 기준으로 업데이트

### P2-3. Worktree report delivery 구현 — implemented

`DeliveryMode.report`는 enum과 API surface에는 있었지만 서버 생성 단계에서
`report delivery mode is not implemented`로 차단되고 있었다.

구현:

- `create_managed_job`에서 `DeliveryMode.report` 차단 제거
- `run_development_task(isolation_mode=worktree, delivery_mode=report)`는
  agent 실행, trusted test, immutable proposal 생성까지만 수행한다.
- report 응답은 `delivery_status=reported`, `proposal_status=proposed`,
  `requires_approval=false`, `applied=false`를 반환한다.
- report mode는 approval, apply, commit, PR을 만들지 않고 원본 workspace를 수정하지
  않는다.
- 단계형 `propose_development_job` / `deliver_development_job`도 report 상태를 명확히
  반환한다.
- worktree는 검토를 위해 보존되며, 사용자가 `abandon_development_job`로 정리하거나
  expiry cleanup 대상이 된다.

### P2-4. Async job cancellation 구현 — implemented

Telegram에서 장기 `Working` 상태가 지속될 때 profile-scoped job을 terminal 상태로
전환할 수 있어야 한다.

구현:

- `JobStore.cancel(job_id, profile, reason)` 추가
- MCP tool `cancel_async_job(job_id, reason?)` 추가
- 취소는 기존 DB CHECK constraint와 호환되도록 `status=failed`,
  `stage=cancelled`로 기록한다.
- async job context에서 실행되는 coding-agent subprocess PID를 process registry에 등록한다.
- `cancel_async_job`은 profile 소유권 확인 후 등록된 process group에 SIGTERM/SIGKILL을
  보낸다.
- cancellation event에는 `process_killed`, `process_kill_guaranteed`,
  `process_count`, `pids`를 기록한다.
- registry 밖에서 daemonize된 하위 process까지 회수한다고 보장하지는 않지만, 늦게
  완료된 worker가 cancelled terminal 상태를 `succeeded`로 덮어쓰지 못하게 한다.

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
