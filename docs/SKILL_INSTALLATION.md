# host-coding-agent skill 설치

공용 agentskills.io 호환 원본은
`skills/host-coding-agent/SKILL.md`다. 두 installer 모두 이 파일을 사용하며 반복 실행해도
같은 이름의 skill만 갱신한다.

## OpenClaw

프로젝트 로컬 skill로 설치:

```sh
./scripts/install-openclaw-skill.sh
```

OpenClaw global skill로 설치:

```sh
./scripts/install-openclaw-skill.sh --global
```

스크립트는 다음 공식 형태로 source root를 전달한다.

```sh
openclaw skills install ./skills/host-coding-agent --as host-coding-agent
```

다른 이름의 실행 파일을 사용해야 하면 `OPENCLAW_BIN`을 설정할 수 있다.

## Hermes

기본 설치 위치는 `${HERMES_HOME:-$HOME/.hermes}/skills/host-coding-agent`다.

```sh
./scripts/install-hermes-skill.sh
```

Named profile은 기존 Hermes profile home 패턴에 맞춰
`HERMES_HOME/profiles/<profile>/skills/host-coding-agent`에 설치한다.

```sh
./scripts/install-hermes-skill.sh --profile dev-bot
```

별도 Hermes root를 명시할 수도 있다.

```sh
./scripts/install-hermes-skill.sh \
  --hermes-home /opt/data \
  --profile dev-bot
```

`HERMES_HOME` 자체가 이미 특정 profile home을 가리키는 실행 환경에서는
`--profile`을 생략한다. 예를 들어 `HERMES_HOME=/opt/data/profiles/dev-bot`이면
`/opt/data/profiles/dev-bot/skills/host-coding-agent`에 설치된다.

Hermes installer는 대상 `SKILL.md`만 임시 파일을 거쳐 원자적으로 교체하고, 대상 경로의
symbolic link는 거부한다. MCP endpoint와 profile별 Bearer credential 등록은 별도 작업이며
skill 파일에 credential을 넣지 않는다.
