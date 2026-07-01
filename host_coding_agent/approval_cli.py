from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .approvals import ApprovalError, ApprovalStore
from .applier import PatchApplier, PatchApplyError
from .artifacts import ArtifactError, ProposalStore
from .config import load_config


def _stores(config_path: Path) -> tuple[ProposalStore, ApprovalStore]:
    config = load_config(config_path)
    artifact_path = config.artifacts.path
    if not artifact_path.is_absolute():
        artifact_path = config_path.parent / artifact_path
    proposals = ProposalStore(
        artifact_path,
        ttl_sec=config.artifacts.proposal_ttl_sec,
        max_diff_chars=config.artifacts.max_diff_chars,
    )
    return proposals, ApprovalStore(artifact_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review and decide host-coding-agent patch proposals."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "config.yaml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--profile", required=True)
    list_parser.add_argument("--status")
    list_parser.add_argument("--limit", type=int, default=20)

    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("proposal_id")
    show_parser.add_argument("--profile", required=True)

    for command in ("approve", "reject", "apply"):
        decision_parser = subparsers.add_parser(command)
        decision_parser.add_argument("proposal_id")
        decision_parser.add_argument("proposal_sha256")
        decision_parser.add_argument("--profile", required=True)
        if command != "apply":
            decision_parser.add_argument("--actor", required=True)
            decision_parser.add_argument("--channel", required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> dict:
    args = build_parser().parse_args(argv)
    config_path = args.config.expanduser().resolve()
    proposals, approvals = _stores(config_path)
    if args.command == "list":
        return {
            "ok": True,
            "approvals": approvals.list(
                profile=args.profile,
                status=args.status,
                limit=args.limit,
            ),
        }
    if args.command == "show":
        proposal = proposals.get(args.proposal_id, profile=args.profile)
        approval = approvals.get_for_proposal(
            args.proposal_id,
            profile=args.profile,
        )
        return {"ok": True, "proposal": proposal, "approval": approval}
    if args.command == "apply":
        config = load_config(config_path)
        return PatchApplier(
            config=config,
            proposals=proposals,
            approvals=approvals,
        ).apply(
            proposal_id=args.proposal_id,
            profile=args.profile,
            proposal_sha256=args.proposal_sha256,
        )
    approval = approvals.decide(
        proposal_id=args.proposal_id,
        profile=args.profile,
        proposal_sha256=args.proposal_sha256,
        approved=args.command == "approve",
        decided_by=args.actor,
        decision_channel=args.channel,
    )
    return {"ok": True, "approval": approval}


def main() -> None:
    try:
        result = run()
    except (ApprovalError, ArtifactError, PatchApplyError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
