from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..benchmark.e1_real_engineering import generate_e1_dataset, run_e1_experiment
from ..benchmark.r1_blind import prepare_r1_blind, run_r1_blind
from ..benchmark.r1_model_packages import (
    build_r1_models,
    render_r1_report,
    unlock_r1_confirmation,
)
from ..benchmark.trusted_fem_campaign_v2 import campaign_status, prepare_campaign
from ..benchmark.trusted_fem_preflight_v3 import run_smoke_e4
from ..benchmark.trusted_fem_multiagent_v4 import (
    prepare_v4_multiagent,
    render_v4_report,
    resume_v4_multiagent,
    run_v4_multiagent,
    solve_v4_multiagent,
)
from ..config import ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare and run the trusted-FEM E1-E4 paper campaign v2.")
    parser.add_argument(
        "command",
        choices=(
            "prepare",
            "status",
            "run-e1",
            "prepare-r1-blind",
            "run-r1-blind",
            "unlock-r1-confirmation",
            "build-r1-models",
            "render-r1-report",
            "smoke-e4",
            "prepare-v4-multiagent",
            "run-v4-multiagent",
            "resume-v4-multiagent",
            "solve-v4-multiagent",
            "render-v4-report",
        ),
    )
    parser.add_argument("--source-root", type=Path, default=ROOT / "core" / "example")
    parser.add_argument("--campaign-root", type=Path)
    parser.add_argument("--run-llm", action="store_true")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--llm-execution-mode",
        choices=("single", "chunked_parallel"),
        default="single",
    )
    parser.add_argument("--datacheck", action="store_true")
    parser.add_argument("--case", choices=("T01", "T02", "T03", "T04"), default="T02")
    parser.add_argument("--solve-static", action="store_true")
    parser.add_argument("--solve-modal", action="store_true")
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--max-llm-calls", type=int, default=12)
    args = parser.parse_args()
    r1_commands = {
        "prepare-r1-blind",
        "run-r1-blind",
        "unlock-r1-confirmation",
        "build-r1-models",
        "render-r1-report",
    }
    v4_commands = {
        "prepare-v4-multiagent",
        "run-v4-multiagent",
        "resume-v4-multiagent",
        "solve-v4-multiagent",
        "render-v4-report",
    }
    if args.campaign_root is None and args.command not in v4_commands:
        args.campaign_root = ROOT / "experiments" / (
            "trusted_fem_v2_r1_models" if args.command in r1_commands else "trusted_fem_v2"
        )
    if args.command == "prepare-v4-multiagent":
        payload = prepare_v4_multiagent(
            args.source_root,
            ROOT / "experiments",
            campaign_root=args.campaign_root,
        )
    elif args.command in {"run-v4-multiagent", "resume-v4-multiagent", "solve-v4-multiagent", "render-v4-report"}:
        if args.campaign_root is None:
            raise SystemExit("--campaign-root is required after prepare-v4-multiagent")
        if args.command == "run-v4-multiagent":
            payload = run_v4_multiagent(args.campaign_root, max_llm_calls=args.max_llm_calls)
        elif args.command == "resume-v4-multiagent":
            payload = resume_v4_multiagent(args.campaign_root, max_llm_calls=args.max_llm_calls)
        elif args.command == "solve-v4-multiagent":
            payload = solve_v4_multiagent(
                args.campaign_root,
                all_cases=args.all_cases,
                solve_static=args.solve_static,
                solve_modal=args.solve_modal,
            )
        else:
            payload = render_v4_report(args.campaign_root)
    elif args.command == "smoke-e4":
        payload = run_smoke_e4(
            args.source_root,
            ROOT / "experiments",
            case=args.case,
            solve_static=args.solve_static,
        )
    elif args.command == "prepare-r1-blind":
        payload = prepare_r1_blind(args.source_root, args.campaign_root)
    elif args.command == "run-r1-blind":
        payload = run_r1_blind(
            args.campaign_root,
            repetitions=args.repetitions,
            execution_mode=args.llm_execution_mode,
        )
    elif args.command == "unlock-r1-confirmation":
        payload = unlock_r1_confirmation(args.campaign_root, args.source_root)
    elif args.command == "build-r1-models":
        payload = build_r1_models(args.campaign_root, datacheck=args.datacheck)
    elif args.command == "render-r1-report":
        payload = render_r1_report(args.campaign_root)
    elif args.command == "prepare":
        payload = prepare_campaign(args.source_root, args.campaign_root)
    elif args.command == "status":
        payload = campaign_status(args.campaign_root)
    else:
        e1_root = args.campaign_root / "E1" / "dataset"
        if not (e1_root / "dataset_manifest.json").is_file():
            generate_e1_dataset(args.source_root, e1_root)
        payload = run_e1_experiment(
            e1_root,
            mode="all" if args.run_llm else "R0",
            r0_repetitions=1,
            r1_repetitions=5,
            run_llm=args.run_llm,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
