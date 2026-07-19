# host-coding-agent-mcp

Docker에서 실행되는 Hermes Agent가 Mac host의 Antigravity, Codex, OpenCode CLI를 호출하도록 연결하는 Streamable HTTP MCP server다.

## 현재 안전 기준

- Legacy `run_coding_agent` 기본 모드는 `propose_patch`이며 원본 파일 쓰기를 금지한다.
- 일반 개발 진입점 `run_development_task`는 profile 기본 isolation mode를 사용한다.
  현재 Hermes profile은 `direct`가 기본이며 허용된 원본 workspace를 즉시 수정한다.
- Read-only/proposal 실행은 Codex CLI read-only sandbox와 OpenCode/Antigravity의
  macOS read-only sandbox를 사용한다.
- Direct/worktree 실행은 profile 권한을 확인한 뒤 해당 workspace만 write sandbox에
  허용한다. OpenCode는 OMO의 task 위임을 사용할 수 있다.
- 일반 MCP `apply_patch` mode는 `allow_apply_patch: false`로 비활성화되어 있으며,
  direct workflow의 내부 승인된 write 경로와 분리되어 있다.
- 모든 HTTP 요청은 profile별 Bearer token 인증이 필요하다.
- Bearer token이 `assistant_id`를 결정하며 요청값으로 다른 profile을 사칭할 수 없다.
- 요청 `cwd`는 전역 allowed root와 인증 profile의 allowed root를 모두 통과해야 한다.
- profile별 허용 agent/mode와 기본 cwd/agent/mode/context를 적용한다.
- subprocess는 shell 없이 argv로 실행하고 timeout 시 process group 전체를 종료한다.
- task와 출력의 secret 패턴을 차단/마스킹한다.
- 호출 메타데이터만 `logs/calls.jsonl`에 `0600` 권한으로 기록한다.

문자열 기반 위험 명령 필터는 보조 통제다. 핵심 통제는 CLI sandbox, allowed root,
profile별 isolation 권한이다.

전체 구성과 요청 흐름은 [아키텍처 문서](docs/ARCHITECTURE.md)에서 확인할 수 있다.
문서 목록은 [docs/README.md](docs/README.md)에 정리되어 있다.

## 설치

```bash
cd ~/host-coding-agent-mcp
mkdir -p ~/projects ~/tmp ~/hermes-workspaces ~/coding
uv sync --extra dev
./scripts/generate-token.sh
./scripts/check.sh
uv run pytest
```

`config.yaml`의 CLI 절대 경로와 allowed roots를 실제 환경에 맞게 확인한다.
Bearer token은 기본적으로 `~/.config/host-coding-agent-mcp/tokens.env`에 mode `0600`으로
생성된다. YAML이나 저장소에는 token 원문을 기록하지 않는다.

## 실행

```bash
./scripts/start.sh
```

기본 endpoint:

```text
http://127.0.0.1:8787/mcp
```

## Skill 설치

공용 AgentSkills 원본은 `skills/host-coding-agent/SKILL.md`다.

```bash
# 현재 OpenClaw workspace
./scripts/install-openclaw-skill.sh

# 모든 local OpenClaw agent
./scripts/install-openclaw-skill.sh --global

# Hermes 기본 profile
./scripts/install-hermes-skill.sh

# Hermes named profile
./scripts/install-hermes-skill.sh --profile dev-bot
```

자세한 경로와 별도 `HERMES_HOME` 사용법은
[skill 설치 문서](docs/SKILL_INSTALLATION.md)를 참고한다. Skill에는 credential을 넣지
않으며 MCP endpoint/Bearer header 등록은 별도로 유지한다.

## Agent 확인과 명시 선택

개발 실행 전에 `check_host_coding_agents`를 호출한다. 응답의
`selectable_agents`만 사용자에게 제시하고 선택된 이름을 실행 요청의 `agent`에 전달한다.
각 `agents` 항목은 `installed`, `enabled`, `profile_allowed`, `selectable`, `version`,
`probe_error`, `unavailable_reason`을 제공한다. `auto`는 기존 자동화 호환용으로 남아 있지만
대화형 skill은 명시 선택을 우선한다.

```text
check_host_coding_agents
→ 사용자에게 antigravity/codex/opencode 중 selectable agent 선택 요청
→ start_development_task(agent="codex", ...)
```

## 비동기 실행과 진행 조회

장기 개발은 동기 `run_development_task` 대신 `start_development_task`를 권장한다. 시작
호출은 즉시 `job_id`를 반환하고 실제 작업은 background worker에서 진행된다.

```json
{
  "task": "인증 모듈을 리팩터링하고 테스트해줘",
  "cwd": "/Users/jaehyunlee/projects/example",
  "agent": "codex",
  "isolation_mode": "worktree",
  "delivery_mode": "manual",
  "timeout_sec": 900
}
```

Polling 절차:

1. `get_async_job(job_id)`로 `queued`, `running`, `succeeded`, `failed` 상태와 현재
   `stage`를 확인한다.
2. `get_async_job_events(job_id, after=<next_after>)`로 새 이벤트만 조회한다.
3. terminal 상태에서는 `job.result`에서 기존 workflow 응답을 읽는다.
4. 호출이 끊겼으면 `list_async_jobs`로 profile의 최근 job ID를 복구한다.

이벤트에는 worktree 생성, agent routing, agent attempt 시작/종료, return code, timeout,
소요시간, test, proposal, delivery 단계가 기록된다. Job과 이벤트는
`artifacts/jobs.db`에 profile별로 격리되어 저장된다. 서버 재시작 전에 실행 중이던 job은
재실행하지 않고 `failed/interrupted`로 표시한다.

문제 진단 시에는 다음을 확인한다.

- `stage=agent_attempt`: `details.agent`, `returncode`, `timed_out`, `duration_sec`
- `stage=test`: 기준 커밋의 project test 실행 중
- `stage=proposal`: immutable proposal 생성 중
- `stage=delivery`: commit/PR delivery 중
- `job.error`: worker 자체 예외이며 secret 패턴은 redaction됨
- `logs/calls.jsonl`: agent 실행 시작/완료 감사 메타데이터

## Docker 연결

권장 방식은 Docker Desktop 4.34+에서 host networking을 활성화하고 Hermes container를 `network_mode: host`로 실행하는 것이다. 이 경우 Hermes에서 `http://127.0.0.1:8787/mcp`를 등록한다.

bridge network의 `host.docker.internal`을 사용하면 host server를 `0.0.0.0`에 bind해야 할 수 있다. Bearer 인증은 적용되어 있지만 Origin/Host 검증과 동시 실행 제한은 아직 없으므로, 방화벽과 Docker network 범위를 함께 제한해야 한다.

Hermes 등록 시 profile에 대응하는 token을 HTTP `Authorization: Bearer <token>` 헤더로
전달해야 한다. token 값은 host의
`~/.config/host-coding-agent-mcp/tokens.env`에서 안전하게 주입하고 명령 이력이나
profile 문서에 평문으로 복사하지 않는다.

`dev-bot`, `invest-bot`, `research-bot`, `youtube-bot`에는 각각 고유 token과
전용 workspace 정책으로 등록되어 있다.

- endpoint: `http://host.docker.internal:8787/mcp`
- credential: `/opt/data/profiles/dev-bot/.env`의
  `MCP_HOST_CODING_AGENT_API_KEY` (`0600`)
- MCP config header: `Authorization: Bearer ${MCP_HOST_CODING_AGENT_API_KEY}`
- 활성 도구: 인증 profile에 허용된 MCP 도구

연결 검증:

```bash
docker exec --user hermes hermes-dev sh -lc '
HERMES_HOME=/opt/data HOME=/opt/data \
/opt/hermes/.venv/bin/hermes -p dev-bot mcp test host-coding-agent
'
```

Hermes의 현재 `mcp add --auth header`는 최초 discovery에서 `${ENV_VAR}`를 치환하지 않는
문제가 있어, 등록 시 Hermes의 `_save_mcp_server`를 사용해 env 참조 설정을 저장하고
`mcp test`로 검증했다. 실제 session의 MCP config loader는 env 참조를 정상 치환한다.

각 profile 연결은 해당 Hermes container에서 `hermes -p <profile> mcp test
host-coding-agent`로 검증한다.

## MCP tools

핵심 도구:

- Discovery/선택: `check_host_coding_agents`
- 비동기 실행: `start_development_task`, `get_async_job`, `get_async_job_events`,
  `list_async_jobs`
- 동기 호환: `run_development_task`, `run_coding_agent`
- 단계별 worktree: `create_development_job`, `run_development_job`,
  `test_development_job`, `propose_development_job`, `deliver_development_job`,
  `get_development_job`, `list_development_jobs`, `abandon_development_job`
- Agent 고정 호환: `run_antigravity`, `run_codex`, `run_opencode`
- Proposal 조회: `get_patch_proposal`, `list_patch_proposals`

`run_coding_agent` 입력:

```json
{
  "task": "healthcheck 실패 원인을 분석하고 unified diff를 제안해줘",
  "cwd": "/Users/jaehyunlee/projects/example",
  "agent": "auto",
  "mode": "propose_patch",
  "timeout_sec": 900,
  "assistant_id": "dev-bot",
  "context": {
    "language": "한국어",
    "environment": "macOS host, Docker에서 호출",
    "runtime": "python",
    "runtime_version": "3.12",
    "framework": "FastAPI",
    "package_manager": "uv",
    "test_command": "uv run pytest"
  }
}
```

`cwd`는 사용자가 원하는 작업 위치이며 기존 allowed root 정책을 통과해야 한다.
추가로 인증 profile의 allowed root도 통과해야 한다. `assistant_id`는 선택 사항이지만
전달할 경우 Bearer token이 나타내는 profile과 일치해야 한다.
`context`는 선택 사항이고 호출마다 다르게 전달할 수 있다.
`context`에는 응답 언어, 실행 환경, runtime, framework, package manager, test command를
구조화해 전달한다. secret 노출을 막기 위해 환경변수 key/value 입력은 지원하지 않는다.
응답에는 실제 적용된 `assistant_id`와 `context`가 포함되며 감사 로그에는 context 원문
대신 hash만 기록한다.

자동 라우팅 정책은 bug/diff는 Codex, 리팩토링·아키텍처·멀티파일 작업은 OpenCode를 우선한다. 일반 분석은 Antigravity를 우선하고 실패 시 Codex, OpenCode 순서로 fallback한다.

Antigravity는 host keychain OAuth로 인증한다. CLI 상태 저장에 필요한
`~/.gemini/antigravity-cli`, `~/.gemini/config`, macOS 임시 디렉터리만 쓰기
허용한다. MCP read-only smoke test와 workspace overwrite/새 파일 생성 차단을
검증했다.

OpenCode 전용 agent는 `openai/gpt-5.4`와 `oh-my-openagent@latest`를 사용한다.
Read-only/proposal mode에서는 OMO의 sub-agent 위임만 허용하고 파일 수정과 shell을
차단한다. Direct/worktree mode에서는 해당 workspace에 한해 수정과 shell을 허용한다.

OMO의 `claude-code-hooks`는 task 원문 transcript를 `~/.claude/transcripts`에 기록하므로 host 설정에서 비활성화한다. MCP 감사 로그는 기존대로 task 원문 대신 hash만 저장한다.

2026-06-24에 host의 OpenAI OAuth 인증을 갱신했고, MCP와 동일한 read-only sandbox 경로의 cloud model smoke test를 통과했다. 이후 `401 authentication token has been invalidated`가 다시 발생하면 host에서 `opencode auth login`을 실행해 OpenAI 로그인을 갱신한다.

## Immutable proposal artifacts

`propose_patch` 실행이 유효한 unified diff를 반환하면 MCP가
`artifacts/proposals.db`에 proposal을 자동 저장한다. DB와 디렉터리는 각각 `0600`,
`0700`이며 proposal row에는 UPDATE/DELETE를 거부하는 SQLite trigger가 적용된다.

저장 항목:

- proposal ID와 diff SHA-256
- 인증 profile, canonical cwd, 선택 agent
- task 원문 대신 SHA-256
- diff 원문
- 대상 파일별 제안 시점 SHA-256
- Git HEAD
- 생성·만료 시각

`get_patch_proposal`은 같은 인증 profile이 소유한 proposal 원문을 조회한다.
`list_patch_proposals`는 목록에서 diff 원문을 제외한다. path traversal, workspace 탈출,
symlink 경로, 빈 diff, 크기 제한 초과 diff는 저장하지 않는다.

proposal 저장 자체는 파일을 변경하거나 승인하지 않는다. Worktree manual proposal은
Telegram approver가 승인한 뒤 제한된 patch applier가 원본에 적용한다. Direct mode는
proposal 경로를 사용하지 않고 원본 workspace를 즉시 수정한다.

## launchd

```bash
./scripts/install-launchd.sh
launchctl print gui/$(id -u)/com.jaehyunlee.host-coding-agent-mcp
```

2026-06-28에 사용자 LaunchAgent 등록과 `RunAtLoad`/`KeepAlive` 동작을 검증했다.
job에 `SIGTERM`을 보낸 뒤 launchd가 새 PID로 서버를 자동 재시작했고, 재시작 후
Bearer 인증과 Hermes `dev-bot` MCP 연결도 정상 동작했다.
launchd의 기본 PATH에는 Node.js가 없으므로 설치 스크립트가 현재 Node.js bin
디렉터리를 plist의 PATH에 포함한다. 기존 job을 완전히 unload할 때까지 기다린 후
새 plist를 bootstrap한다.

2026-06-30에 Telegram dev-bot 요청이 Bearer 인증 MCP를 거쳐 Codex를 실행하고
Telegram 세션으로 반환되는 end-to-end 경로를 검증했다.

로그:

- `logs/server.out.log`
- `logs/server.err.log`
- `logs/calls.jsonl`

## Hermes profile 지침

모든 profile에 `development-policy` Hermes plugin을 설치한다. 이 plugin은 매 turn에
MCP routing 정책을 주입하고 `pre_tool_call`에서 native 실행·쓰기 도구를 dispatch 전에
차단한다.

```bash
./scripts/install-hermes-policy.sh hermes-dev dev-bot
./scripts/install-hermes-policy.sh hermes-invest invest-bot
./scripts/install-hermes-policy.sh hermes-research research-bot
./scripts/install-hermes-policy.sh hermes-youtube youtube-bot
```

차단 도구:

- `terminal`
- `execute_code`
- `write_file`
- `patch`
- `delegate_task`

허용되는 개발 경로는 `mcp_host_coding_agent_*` 도구다. MCP 실패 시 native 도구로
fallback하지 않고 오류를 보고한다. 정책은 fail-closed이므로 plugin이 활성화된
profile에서는 비개발 목적이라도 위 native 도구를 사용할 수 없다.

정책 명세는 `docs/DEVELOPMENT_ENFORCEMENT.md`, 배포 원본은
`hermes_plugins/development-policy`에 있다. installer는 plugin enable과 SOUL 정책
block을 idempotent하게 적용한다. Policy hooks는 profile plugin에, Telegram slash
commands는 gateway의 global plugin registry에도 배포한다. 적용 후 해당 Hermes
gateway/container를 재시작한다.

## Profile 설정

`config.yaml`의 `profiles`에서 profile별 정책을 선언한다.

```yaml
profiles:
  dev-bot:
    token_env: HOST_CODING_AGENT_DEV_BOT_TOKEN
    allowed_roots:
      - /Users/jaehyunlee/projects
    allowed_container_roots:
      - /opt/data/profiles/dev-bot/workspace
    runtime_labels:
      com.docker.compose.service: hermes-dev
    allowed_agents: [antigravity, codex, opencode]
    allowed_modes: [read_only, propose_patch]
    default_cwd: /opt/data/profiles/dev-bot/workspace
    default_agent: auto
    default_mode: propose_patch
    context:
      language: 한국어
      runtime: python
      package_manager: uv
```

호출값이 생략되면 profile 기본값을 사용한다. context는
`호출값 > profile 기본값` 순서로 병합하며, 전역 security 설정은 모든 profile에
적용되는 상한선이다. profile 추가 시 고유한 `token_env`를 지정하고 해당 환경변수에는
최소 32자 이상의 고유 token을 설정한다.

`allowed_roots`는 host에서 직접 전달할 수 있는 경로이고,
`allowed_container_roots`는 container 내부에서 허용할 workspace 경로다. Hermes
gateway plugin은 LLM 호출 전에 `/etc/hostname`의 container ID를 host에 등록한다.
Host MCP는 `docker inspect`의 bind mount 목록에서 가장 긴 `Destination` prefix를
선택하고 `Source`를 조합해 실제 host 경로를 계산한다. `/opt/data` 전체는 허용하지
않는다. Hermes의 `mcp_servers.host-coding-agent.timeout`은 장기 agent 실행을
고려해 900초로 설정한다.
`runtime_labels`는 인증 profile이 다른 container ID를 등록하지 못하도록 Docker
metadata를 검증한다. 인증된 registration은 `artifacts/runtimes.json`에 `0600`으로
저장되며 MCP 재시작 시 container 실행 상태·label·mount를 다시 inspect한 후 복원한다.
실행 결과의 `cwd`는 변환된 macOS host 경로이며 정상 동작이다. 입력값은
`requested_cwd`, 변환 여부는 `path_mapping_applied`로 함께 반환한다. 전달된 경로가
허용된 host 경로로 존재하면 변환하지 않고 직접 사용한다.

`approval_identities`에는 해당 profile의 proposal을 승인할 수 있는 외부 identity만
등록한다. 예: `telegram:7965486003`. 일반 MCP agent profile의 `allowed_modes`에는
`apply_patch`를 추가하지 않는다. 실제 적용 권한은 LLM-visible MCP tool이 아니라
`/approval/telegram` 외부 endpoint의 제한된 applier에만 있다.

Telegram 승인 명령:

- `/proposal <proposal_id>`: immutable diff와 hash 검토
- `/apply_proposal <proposal_id> <proposal_sha256>`: 승인, 재검증, patch 적용
- `/reject <proposal_id> <proposal_sha256>`: 거절

승인 적용 시 profile/token/Telegram user/hash/만료/Git HEAD/base file/path/symlink를
재검증하고 `git apply --check` 후 적용한다. 승인 상태와 이벤트는 SQLite에 기록되며
재사용할 수 없다.
Proposal 저장 전에는 unified diff의 마지막 newline과 whitespace-only 추가 줄을
정규화하고, workspace 내부 절대 header를 상대경로로 바꾼 뒤
`git apply --check --recount`를 통과해야만 proposal ID를 발급한다.

## 알려진 제한

- Origin/Host 검증과 동시 실행 제한은 아직 구현하지 않았다.
- timeout은 process group을 종료하지만 스스로 새 session으로 daemonize한 하위 프로세스까지 완전하게 회수하지는 못한다. 장기 작업 queue 전에 별도 worker 격리가 필요하다.
- Hermes의 기존 MCP timeout이 900초여도 비동기 시작 호출은 빠르게 반환한다. 실제 coding-agent timeout은 job 입력의 `timeout_sec`에 적용된다.
- Docker container 호출은 host의 Docker Engine과 `docker inspect` 권한이 필요하다.

## Worktree 개발 격리

Worktree 기반 자동개발의 내부 기반으로 `WorktreeManager`를 제공한다. 대상 Git
저장소의 현재 HEAD에서 `hca/<profile>/<job-id>` branch와
`~/.cache/host-coding-agent/worktrees/<job-id>` 작업공간을 생성하고, 작업 identity를
`artifacts/worktrees.db`에 기록한다. 원본 workspace 파일은 변경하지 않는다.

현재 단계에서는 MCP tool로 노출하지 않는다. Dirty workspace, repository lock,
worktree 전용 agent write sandbox와 test runner가 구현된 뒤 개발 요청에 연결한다.

Worktree 생성 전에는 tracked/untracked 변경과 merge, rebase, cherry-pick, revert,
bisect 진행 상태를 검사한다. Repository별 DB lock으로 비종료 작업을 하나만 허용하고,
작업이 `delivered`, `failed`, `abandoned` 상태가 되면 lock을 해제한다.

내부 `run_managed_worktree_agent`는 manager가 job ID, profile, branch, base commit과
관리 root를 재검증한 뒤에만 write 실행을 허용한다. Codex는 자체
`workspace-write` sandbox를 사용하고 OpenCode/Antigravity는 macOS `sandbox-exec`로
worktree와 필수 cache/temp 경로만 쓸 수 있다. 일반 MCP `apply_patch` mode는 계속
비활성화되어 있다.

개발 실행 후 `run_managed_worktree_tests`는 agent가 수정한 worktree의 정책 파일이
아니라 기준 커밋의 `.host-coding-agent.yaml`을 읽는다. 명령은 shell 문자열이 아닌
argv 배열로 선언하며 순서대로 실행한다. 모든 명령이 성공해야 작업이 `tested`가 되고,
실패·timeout·정책 오류는 `failed`로 전환한다. 명령별 출력과 종료 상태는 append-only
test run record로 저장된다.

테스트를 통과한 작업은 내부 `create_managed_worktree_proposal`로 원본 저장소의
base commit과 worktree 전체 변경을 비교한다. 임시 Git index를 사용하므로 worktree의
실제 index를 변경하지 않으며, 수정·삭제·untracked·binary 파일을 하나의 unified diff에
포함한다. 생성된 proposal의 적용 대상은 임시 worktree가 아니라 원본 저장소이고,
task hash와 proposal ID/SHA-256 연결은 불변 레코드로 저장된다. 원본 HEAD가 바뀌거나
dirty 상태이면 proposal을 만들지 않고 작업을 `failed`로 종료한다.

manual delivery 작업의 proposal에는 pending approval이 함께 생성된다. Telegram
`/apply_proposal` 승인 요청이 들어오면 기존 patch 검증·적용·audit을 수행하고 작업을
`delivered`로 전환한다. 이후 dirty worktree를 강제로 제거하고 작업용 branch도
삭제한다. Cleanup 결과는 append-only audit record로 남으며, 적용 실패 시 작업은
`failed`로 전환해 repository lock을 해제하되 조사 가능한 worktree는 보존한다.
적용은 성공했지만 상태 응답이 중단된 경우에는 approval의 `applied` 상태를 기준으로
delivery를 재개할 수 있다.

`commit`, `auto`, `pr` 작업은 approval을 만들지 않고 immutable proposal과 현재
worktree diff가 일치하는지 다시 확인한 후 delivery한다. `commit`은 작업 branch에
로컬 커밋을 만들고 worktree만 제거하여 branch를 저장소에 남긴다. `pr`은 job 생성
시점에 고정한 remote 이름·URL과 base branch를 재검증하고, profile에서 허용한 GitHub
remote에만 push한 뒤 `gh pr create`를 실행한다. 성공 후 로컬 worktree와 작업 branch를
제거한다. `auto`는 push/PR 권한, 허용 remote, base branch가 모두 유효할 때만 `pr`을
선택하고 그 외에는 `commit`으로 제한한다. Remote가 없으면 `commit`은 정상 동작하고
명시적 `pr`은 적용 전에 실패한다.

Fetch URL과 push URL은 각각 job metadata에 고정하며 둘이 동일한 GitHub 저장소를
가리키는지 확인한다. Commit/Push 시 repository hook과 commit signing은 실행하지 않는다.

```yaml
profiles:
  dev-bot:
    allowed_delivery_modes: ["manual", "auto", "commit", "pr"]
    allowed_remote_names: ["origin"]
    allowed_remote_hosts: ["github.com"]
    allow_git_push: true
    allow_pull_requests: true
    allowed_isolation_modes: ["direct", "worktree"]
    default_isolation_mode: "direct"
    git_author_name: "host-coding-agent"
    git_author_email: "host-coding-agent@localhost"
```

PR 기능은 host에서 인증된 `gh` CLI와 해당 remote에 대한 Git push 권한이 필요하다.
기본 설정에서는 push와 PR 생성이 모두 비활성화되어 있다.

### Development MCP API

일반 개발 요청에는 단일 `run_development_task` MCP tool을 사용한다. 이 도구가
`direct` 또는 `worktree` 격리 방식을 profile 정책에 따라 선택한다.

- `direct`: Git 없이 허용된 원본 workspace에서 coding agent가 즉시 수정·테스트한다.
  Worktree, proposal, commit, approval은 만들지 않는다.
- `worktree`: 원본과 격리해 개발·신뢰된 테스트·immutable proposal을 처리한다.
  `manual`은 Telegram 승인을 기다리고 `commit`/`auto`/`pr`은 delivery까지 진행한다.

호출 예:

```json
{
  "task": "로그인 오류를 수정하고 테스트해줘",
  "cwd": "/opt/data/profiles/dev-bot/workspace",
  "agent": "opencode",
  "isolation_mode": "direct",
  "timeout_sec": 900
}
```

Telegram에서는 다음 정도의 자연어 요청이면 충분하다.

```text
OpenCode로 현재 프로젝트의 로그인 오류를 수정하고 테스트해줘.
host-coding-agent MCP의 run_development_task를 direct 모드로 사용해.
```

Direct는 원본을 즉시 수정하므로 적용 승인과 자동 rollback이 없다. 변경 전 검토,
실패 격리, 승인 적용이 필요하면 다음처럼 worktree를 요청한다.

```text
OpenCode로 로그인 오류를 수정하고 테스트해줘.
run_development_task의 isolation_mode는 worktree,
delivery_mode는 manual로 실행하고 적용 승인을 요청해.
```

세부 제어·재시작·상태 조회가 필요한 경우 다음 단계별 tool을 사용한다.

1. `create_development_job`: 대상 Git 저장소와 delivery mode를 고정하고 worktree 생성
2. `run_development_job`: 동일 task를 전달해 허용된 coding agent를 worktree에서 실행
3. `test_development_job`: base commit의 신뢰된 test policy 실행
4. `propose_development_job`: 테스트된 변경을 immutable proposal로 저장
5. `deliver_development_job`: commit/auto/PR delivery 실행. Manual은 approval 정보 반환
6. `get_development_job`, `list_development_jobs`: profile 소유 job 상태 조회
7. `abandon_development_job`: 미완료/실패 job의 lock 해제와 worktree 정리

각 단계는 job ID와 인증 profile을 함께 검증한다. 실행 시 task가 job 생성 당시의
SHA-256과 다르거나 proposal 생성 후 worktree가 변경되면 다음 단계로 진행하지 않는다.
다른 profile은 job ID를 알아도 조회하거나 실행할 수 없다. Manual proposal 거절 시
job은 `abandoned`가 되고 worktree와 branch가 정리된다.

단계별 API는 동기 호환 인터페이스다. client timeout보다 긴 전체 workflow에는 위의
`start_development_task`와 polling API를 사용한다. 기본 coding-agent timeout은 900초다.

```yaml
version: 1
tests:
  timeout_sec: 300
  commands:
    - ["uv", "run", "pytest", "-q"]
```

- 비동기 job의 최종 `result`가 매우 크면 MCP client 응답 크기 제한에 걸릴 수 있다.
  현재 이벤트는 cursor pagination을 지원하지만 result 자체의 pagination은 지원하지 않는다.
