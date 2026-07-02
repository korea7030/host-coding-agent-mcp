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
| Antigravity | O | O | OAuth 인증 및 이중 sandbox 검증 완료 |
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

### Antigravity 격리

Antigravity에는 다음 보호를 함께 적용한다.

1. CLI `--sandbox`
   - terminal 명령 제한
2. macOS `sandbox-exec`
   - workspace와 일반 host 파일 쓰기 차단
   - OAuth/config/state 저장소와 macOS 임시 디렉터리만 쓰기 허용

host keychain OAuth 인증, 모델 호출, workspace 읽기, overwrite 차단, 새 파일 생성 차단을
검증했다. CLI의 `--print`가 바로 다음 인자를 prompt로 소비하므로 runner의 argv 순서도
수정했다.

## 3. 검증 결과

- 전체 테스트: `38 passed`
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
- Hermes container → host MCP 연결 및 도구 조회 성공
- launchd 사용자 LaunchAgent 등록 및 `RunAtLoad` 기동 확인
- launchd `KeepAlive` 프로세스 자동 재시작과 인증 상태 유지 확인
- Hermes invest/research/youtube profile별 고유 token과 workspace 정책 등록 완료
- Hermes 4개 profile 모두 Bearer 인증 연결 및 도구 조회 성공
- Antigravity OAuth, 이중 sandbox, MCP read-only smoke test 성공 (`AGY_MCP_OK`)
- Antigravity workspace overwrite 및 새 파일 생성 차단 확인
- Telegram dev-bot → Bearer 인증 MCP → Codex → Telegram 세션 E2E 성공
- launchd Node.js PATH 누락으로 발생한 Codex return code 127 수정
- Hermes `development-policy` pre-tool hook 구현
- native terminal/code execution/file mutation/delegation dispatch 차단
- MCP routing 정책 turn별 주입 및 SOUL 상시 정책 적용
- dev-bot canary에서 terminal/write 차단, 파일 미생성, MCP 허용 검증
- MCP 실패 후 native terminal fallback 차단 확인
- Hermes 4개 profile에 development-policy 배포 완료
- propose_patch 결과의 immutable SQLite artifact 자동 저장 구현
- proposal diff/task/base file SHA-256, Git HEAD, profile, cwd, 만료 시각 저장
- SQLite UPDATE/DELETE trigger 기반 proposal 불변성 확인
- proposal path traversal·workspace 탈출·symlink·크기 제한 검증
- profile별 `get_patch_proposal`, `list_patch_proposals` 접근 격리
- 실제 Codex diff artifact 저장 및 원본 workspace 무변경 확인
- gateway container ID 등록과 Docker inspect 기반 workspace 자동 매핑 구현
- longest mount destination prefix 선택 및 bind mount source 검증
- host 직접 cwd와 container cwd 자동 판별 구현
- profile별 Docker label 검증과 runtime registration 재시작 복원 구현
- diff transport newline·whitespace-only line·absolute header 정규화 구현
- proposal 발급 전 `git apply --check --recount` preflight 구현
- workspace 내부 absolute diff path 정규화로 proposal artifact 생성 실패 수정
- Hermes 4개 profile의 host-coding-agent MCP timeout 900초 정렬
- profile/token/Telegram user allowlist 기반 외부 승인 endpoint 구현
- Telegram `/proposal`, `/apply_proposal`, `/reject` gateway command 구현
- profile policy plugin과 global gateway command plugin 이중 배포
- 승인 상태 전이, 만료, 재사용 차단과 append-only audit event 구현
- Git HEAD/base hash/path/symlink/binary patch 및 `git apply --check` 검증
- 적용 결과 hash 저장과 audit 완료 실패 시 reverse patch rollback 구현
- 실행 결과에 container 요청 cwd와 host 변환 여부를 명시적으로 반환
- coding agent에 resolved host cwd와 상대경로만 사용하도록 prompt 강제
- 실패 audit에 timeout 여부와 redacted stderr preview 추가

라우팅 예:

| 요청 유형 | 실행 순서 |
|---|---|
| 설정 확인, 일반 분석 | Antigravity → Codex → OpenCode |
| 버그 수정, diff 생성 | Codex → OpenCode |
| 멀티파일 리팩토링, 테스트 작성 | OpenCode → Codex |

## 4. 현재 제한사항

- Agent 인증 여부는 상태 조회 결과에 아직 포함되지 않는다.
- daemonized child process가 별도 session을 만들면 timeout 종료 범위를 벗어날 수 있다.
- Origin/Host 검증과 동시 실행 제한은 아직 없다.
- development-policy는 fail-closed로 native 실행·쓰기 도구를 항상 차단하므로 비개발
  terminal/file-write 작업도 수행할 수 없다.
- 대용량 동기 결과는 호출 client의 응답 크기/시간 제한을 받을 수 있다.

## 5. 다음 작업

1. Origin/Host 검증과 동시 실행 제한
2. 실제 macOS 재부팅 후 launchd `RunAtLoad` 최종 확인
3. Agent 인증 상태 health check
4. 비동기 job queue, job status와 paginated result 조회 구현
5. 승인 후 profile별 허용 test command 실행 정책 구현

## 6. 주요 경로

- 프로젝트: `/Users/jaehyunlee/host-coding-agent-mcp`
- 서버 진입점: `/Users/jaehyunlee/host-coding-agent-mcp/server.py`
- 설정: `/Users/jaehyunlee/host-coding-agent-mcp/config.yaml`
- 로그: `/Users/jaehyunlee/host-coding-agent-mcp/logs/calls.jsonl`
- 테스트: `/Users/jaehyunlee/host-coding-agent-mcp/tests`
