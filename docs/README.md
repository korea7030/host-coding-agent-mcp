# Documentation

- [Architecture](ARCHITECTURE.md): 전체 구성, direct/worktree 실행 흐름, 경로 매핑,
  저장소와 신뢰 경계
- [Agent routing](AGENT_ROUTING.md): 요청을 받을 때 OpenCode, Codex,
  Antigravity 중 어떤 agent를 호출할지 결정하는 과정
- [Development enforcement](DEVELOPMENT_ENFORCEMENT.md): Hermes가 개발 작업을
  host-coding-agent MCP로만 전달하도록 강제하는 정책
- [Execution reliability plan](EXECUTION_RELIABILITY_PLAN.md): runtime registration,
  sandbox, direct/worktree 조합, proposal 상태 혼동을 줄이기 위한 단계별 구현 계획
- [Skill installation](SKILL_INSTALLATION.md): 공용 agentskills.io skill을 OpenClaw와
  Hermes 기본/named profile에 설치하는 방법
