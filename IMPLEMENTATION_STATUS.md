# host-coding-agent-mcp 구현 현황

작성일: 2026-06-28

## 1. 구현 완료 내용

현재 `/Users/jaehyunlee/host-coding-agent-mcp`에 MVP가 구현되어 있다.

### MCP 서버

- FastMCP 기반 Streamable HTTP 서버
- 기본 주소: `http://127.0.0.1:8787/mcp`
- Docker 컨테이너에서는 `host.docker.internal`을 통해 접근하도록 설계
- profile별 opaque Bearer token 인증

### 제공 도구

- `check_host_coding_agents`
- `run_coding_agent`
- `run_antigravity`
- `run_codex`
- `run_opencode`

모든 실행 도구는 호출별 `cwd`, `assistant_id`, 구조화된 `context`를 입력받는다.
`context`는 응답 언어, 실행 환경, runtime/version, framework, package manager,
test command를 agent prompt에 명시적인 요구사항으로 전달한다.

### Profile 권한 및 기본값

- Bearer token을 profile identity로 매핑
- token 원문은 YAML이 아닌 profile별 환경변수에서 로드
- `assistant_id` 사칭 차단
- profile별 allowed root, agent, mode 강제
- profile별 기본 cwd, agent, mode, context 적용
- context 병합 우선순위: 호출값 → profile 기본값
- 전역 security 정책은 모든 profile의 상한선으로 유지

### Agent 자동 라우팅

- 일반 분석, 명확한 버그 수정, diff 생성: Codex 우선
- 리팩토링, 멀티파일 변경, 테스트 작성, 구조 변경: OpenCode 우선
- 선택한 Agent가 실패하면 다음 Agent로 fallback
- 비활성화된 Agent는 자동으로 건너뜀

### 실행 및 보안 정책

- 기본 실행 모드: `propose_patch`
- `apply_patch`는 설정상 기본 비활성화
- 허용 workspace root 제한
- `realpath` 기반 경로 검증
- symlink를 이용한 허용 경로 탈출 차단
- 금지 경로 접근 차단
- Secret 및 위험 명령 패턴 탐지
- stdout/stderr에 포함된 token과 secret 마스킹
- subprocess timeout 처리
- process group 단위 종료
- subprocess 실행 시 shell 미사용

### 감사 로그

- 모든 호출을 JSONL로 기록
- 기본 로그: `logs/calls.jsonl`
- 로그 파일 권한: `0600`
- task 원문 대신 hash 중심으로 기록
- Agent 선택, fallback, 실행 시간, 결과 코드 기록

### 운영 파일

- `config.yaml`
- `scripts/start.sh`
- `scripts/check.sh`
- `scripts/install-launchd.sh`
- launchd 설정 파일
- 테스트 코드 및 README

## 2. Coding Agent 상태

| Agent | 설치 | 활성화 | 현재 상태 |
|---|---:|---:|---|
| Antigravity | O | X | 인증과 filesystem 격리를 함께 보장하기 어려워 보류 |
| Codex | O | O | 사용 가능 |
| OpenCode | O | O | OpenAI 인증 및 격리 정책 검증 완료 |

### OpenCode 격리

OpenCode에는 다음 두 단계 보호가 적용되어 있다.

1. OpenCode permission policy
   - 파일 수정 차단
   - shell 명령 실행 차단
   - 외부 경로 접근 차단
   - web 접근 차단
   - Oh My OpenAgent(OMO) plugin 및 sub-agent 위임 허용
2. macOS `sandbox-exec`
   - 프로젝트와 일반 host 파일 쓰기 차단
   - OpenCode 캐시, 상태 저장소 및 임시 디렉터리만 쓰기 허용

2026-06-24에 host의 OpenAI OAuth 인증을 갱신했고, MCP와 동일한
`sandbox-exec` + read-only agent 경로에서 `openai/gpt-5.4` 호출을 검증했다.

### Antigravity 보류 사유

Antigravity의 `--sandbox` 옵션만으로는 다음 권한을 명확하게 분리해 검증하기 어렵다.

- CLI 인증에 필요한 OAuth 및 설정 파일 접근
- Agent가 수행하는 filesystem 및 terminal 접근

CLI도 현재 로그인되지 않은 상태다. 인증과 강제 filesystem 격리를 함께 검증하기 전까지 비활성 상태로 유지한다.

## 3. 검증 결과

- 전체 테스트: `23 passed`
- FastMCP 서버 실제 기동 확인
- MCP tool 목록 조회 확인
- Agent 설치 상태 조회 확인
- 허용 root 검증 통과
- symlink 경로 탈출 차단 테스트 통과
- sandbox 내 workspace 쓰기 차단 확인
- `/private/tmp` 쓰기 허용 확인
- 자동 라우팅 및 fallback 순서 확인
- OpenCode OpenAI OAuth 갱신 완료
- OpenCode cloud model read-only smoke test 성공 (`OPENCODE_OK`)
- OpenCode OMO `ultrawork` read-only smoke test 성공 (`OMO_OK`)
- MCP 실행 도구의 assistant별 위치·언어·환경 context 스키마 확인
- 무토큰 HTTP 요청 `401 Unauthorized` 확인
- 유효 Bearer token MCP 도구 접근 확인
- profile 외 agent/mode/cwd 및 `assistant_id` 사칭 차단 확인
- Hermes `dev-bot` profile에 Bearer 인증 MCP 등록 완료
- Hermes container → host MCP 연결 및 5개 도구 조회 성공

라우팅 예:

| 요청 유형 | 실행 순서 |
|---|---|
| 설정 확인, 일반 분석 | Codex → OpenCode |
| 버그 수정, diff 생성 | Codex → OpenCode |
| 멀티파일 리팩토링, 테스트 작성 | OpenCode → Codex |

## 4. 현재 제한사항

- Antigravity는 비활성 상태다.
- Agent 인증 여부는 상태 조회 결과에 아직 포함되지 않는다.
- daemonized child process가 별도 session을 만들면 timeout 종료 범위를 벗어날 수 있다.
- Hermes의 invest/research/youtube profile 등록과 Telegram end-to-end 검증은 아직 수행하지 않았다.
- Origin/Host 검증과 동시 실행 제한은 아직 없다.

## 5. 다음 작업

1. Telegram dev-bot 요청부터 결과 반환까지 end-to-end 검증
2. Hermes invest/research/youtube profile별 정책·token·MCP 등록
3. Origin/Host 검증과 동시 실행 제한
4. launchd 등록 및 재부팅 후 자동 실행 검증
5. Antigravity 로그인 및 격리 방안 검증
6. 비동기 job queue와 job status 조회 구현
7. 사용자 승인 기반 `apply_patch` 구현
8. 결과 diff 및 artifact 저장 기능 구현

## 6. 주요 경로

- 프로젝트: `/Users/jaehyunlee/host-coding-agent-mcp`
- 서버 진입점: `/Users/jaehyunlee/host-coding-agent-mcp/server.py`
- 설정: `/Users/jaehyunlee/host-coding-agent-mcp/config.yaml`
- 로그: `/Users/jaehyunlee/host-coding-agent-mcp/logs/calls.jsonl`
- 테스트: `/Users/jaehyunlee/host-coding-agent-mcp/tests`
