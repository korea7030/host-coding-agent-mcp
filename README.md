# host-coding-agent-mcp

Docker에서 실행되는 Hermes Agent가 Mac host의 Antigravity, Codex, OpenCode CLI를 호출하도록 연결하는 Streamable HTTP MCP server다.

## 현재 안전 기준

- 기본 모드는 `propose_patch`이며 원본 파일 쓰기를 금지한다.
- Codex는 `--sandbox read-only`로 실행한다.
- Codex는 CLI read-only sandbox로 활성화한다.
- OpenCode는 Oh My OpenAgent(OMO)를 로드하고 `task` 위임을 허용한다. 전용 inline agent에서 `edit`, `bash`, `external_directory`를 deny하며 macOS sandbox에서 일반 파일 쓰기를 차단한다.
- Antigravity는 terminal sandbox와 OAuth 접근을 분리하는 세분화 정책이 확인되지 않아 기본 비활성화한다.
- `apply_patch`는 `config.yaml`의 `allow_apply_patch: false`로 비활성화되어 있다.
- 모든 HTTP 요청은 profile별 Bearer token 인증이 필요하다.
- Bearer token이 `assistant_id`를 결정하며 요청값으로 다른 profile을 사칭할 수 없다.
- 요청 `cwd`는 전역 allowed root와 인증 profile의 allowed root를 모두 통과해야 한다.
- profile별 허용 agent/mode와 기본 cwd/agent/mode/context를 적용한다.
- subprocess는 shell 없이 argv로 실행하고 timeout 시 process group 전체를 종료한다.
- task와 출력의 secret 패턴을 차단/마스킹한다.
- 호출 메타데이터만 `logs/calls.jsonl`에 `0600` 권한으로 기록한다.

문자열 기반 위험 명령 필터는 보조 통제다. 핵심 통제는 CLI sandbox, allowed root, apply 비활성화다.

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

## Docker 연결

권장 방식은 Docker Desktop 4.34+에서 host networking을 활성화하고 Hermes container를 `network_mode: host`로 실행하는 것이다. 이 경우 Hermes에서 `http://127.0.0.1:8787/mcp`를 등록한다.

bridge network의 `host.docker.internal`을 사용하면 host server를 `0.0.0.0`에 bind해야 할 수 있다. Bearer 인증은 적용되어 있지만 Origin/Host 검증과 동시 실행 제한은 아직 없으므로, 방화벽과 Docker network 범위를 함께 제한해야 한다.

Hermes 등록 시 profile에 대응하는 token을 HTTP `Authorization: Bearer <token>` 헤더로
전달해야 한다. token 값은 host의
`~/.config/host-coding-agent-mcp/tokens.env`에서 안전하게 주입하고 명령 이력이나
profile 문서에 평문으로 복사하지 않는다.

`dev-bot`에는 다음과 같이 등록되어 있다.

- endpoint: `http://host.docker.internal:8787/mcp`
- credential: `/opt/data/profiles/dev-bot/.env`의
  `MCP_HOST_CODING_AGENT_API_KEY` (`0600`)
- MCP config header: `Authorization: Bearer ${MCP_HOST_CODING_AGENT_API_KEY}`
- 활성 도구: 5개 전체

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

invest/research/youtube profile은 각 profile 정책과 별도 token을 만든 후 독립적으로
등록한다.

## MCP tools

- `check_host_coding_agents`
- `run_coding_agent`
- `run_antigravity`
- `run_codex`
- `run_opencode`

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

자동 라우팅 정책은 bug/diff는 Codex, 리팩토링·아키텍처·멀티파일 작업은 OpenCode를 우선한다. 일반 분석은 비활성 Antigravity를 건너뛰어 Codex로 실행된다.

OpenCode 전용 agent는 `openai/gpt-5.4`와 `oh-my-openagent@latest`를 사용한다. OMO의 sub-agent 위임은 허용하지만 파일 수정과 shell 실행은 차단한다. OMO orchestration을 명시적으로 요청하려면 task에 `ultrawork`를 포함한다.

OMO의 `claude-code-hooks`는 task 원문 transcript를 `~/.claude/transcripts`에 기록하므로 host 설정에서 비활성화한다. MCP 감사 로그는 기존대로 task 원문 대신 hash만 저장한다.

2026-06-24에 host의 OpenAI OAuth 인증을 갱신했고, MCP와 동일한 read-only sandbox 경로의 cloud model smoke test를 통과했다. 이후 `401 authentication token has been invalidated`가 다시 발생하면 host에서 `opencode auth login`을 실행해 OpenAI 로그인을 갱신한다.

## launchd

```bash
./scripts/install-launchd.sh
launchctl print gui/$(id -u)/com.jaehyunlee.host-coding-agent-mcp
```

로그:

- `logs/server.out.log`
- `logs/server.err.log`
- `logs/calls.jsonl`

## Hermes profile 지침

각 profile의 system instruction에 다음 정책을 추가한다.

```text
host 프로젝트 코드 작업에는 MCP server host-coding-agent를 사용한다.
기본 호출은 run_coding_agent(agent="auto", mode="propose_patch")다.
대규모 리팩토링, 테스트 작성, 구조 변경, migration, multi-file 작업은 OpenCode를 우선한다.
사용자가 명시적으로 승인하기 전에는 apply_patch를 요청하지 않는다.
결과는 요약, 변경 계획, diff, 테스트 방법 순서로 보고한다.
```

## Profile 설정

`config.yaml`의 `profiles`에서 profile별 정책을 선언한다.

```yaml
profiles:
  dev-bot:
    token_env: HOST_CODING_AGENT_DEV_BOT_TOKEN
    allowed_roots:
      - /Users/jaehyunlee/projects
    allowed_agents: [codex, opencode]
    allowed_modes: [read_only, propose_patch]
    default_cwd: /Users/jaehyunlee/projects
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

## 알려진 제한

- Origin/Host 검증과 동시 실행 제한은 아직 구현하지 않았다.
- Antigravity는 별도 macOS 실행 사용자 또는 검증 가능한 terminal permission 정책을 추가하기 전까지 활성화하지 않는다.
- timeout은 process group을 종료하지만 스스로 새 session으로 daemonize한 하위 프로세스까지 완전하게 회수하지는 못한다. 장기 작업 queue 전에 별도 worker 격리가 필요하다.
- 비동기 queue, job id, artifact 저장, approval flow는 후속 단계다.
