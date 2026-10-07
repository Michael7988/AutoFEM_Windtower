from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..benchmark.campaign import ExperimentCampaignConfig, run_experiment_campaign


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Run the auditable wind-tower CAE stability and robustness campaign."
    )
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--cases",
        nargs="+",
        default=[f"S{index:02d}" for index in range(1, 11)],
    )
    parser.add_argument(
        "--stages",
        nargs="+",
        choices=("G1", "G2", "G3", "G4", "G5", "G6"),
        default=("G1", "G2", "G3", "G4", "G5"),
    )
    parser.add_argument("--deterministic-repetitions", type=int, default=10)
    parser.add_argument("--ir-repetitions", type=int, default=10)
    parser.add_argument("--compiler-repetitions", type=int, default=3)
    parser.add_argument(
        "--global-mesh-factors",
        nargs="+",
        type=float,
        default=(1.5, 1.0, 0.75),
    )
    parser.add_argument(
        "--door-mesh-factors",
        nargs="+",
        type=float,
        default=(1.5, 1.0, 0.75),
    )
    parser.add_argument("--llm-repetitions", type=int, default=5)
    parser.add_argument("--modification-repetitions", type=int, default=3)
    parser.add_argument(
        "--run-llm",
        action="store_true",
        help="Explicitly permit paid/cloud LLM calls.",
    )
    parser.add_argument(
        "--run-abaqus",
        action="store_true",
        help="Explicitly permit Abaqus execution for campaigns containing the abaqus stage.",
    )
    parser.add_argument(
        "--compile-modifications",
        action="store_true",
        help="Compile matched direct/natural-language modification candidates to INP.",
    )
    parser.add_argument(
        "--allow-direct-inp-ablation",
        action="store_true",
        help="Permit isolated static checking/Datacheck of externally supplied direct-LLM INP artifacts.",
    )
    parser.add_argument(
        "--include-internal-engineering",
        action="store_true",
        help="Use the five internal G1 engineering references instead of public synthetic fixtures.",
    )
    parser.add_argument("--campaign-name", default="wind_tower_llm_assisted_fem_v1")
    args = parser.parse_args()
    config = ExperimentCampaignConfig(
        campaign_name=args.campaign_name,
        case_ids=[value.upper() for value in args.cases],
        stages=list(args.stages),
        deterministic_repetitions=args.deterministic_repetitions,
        ir_repetitions=args.ir_repetitions,
        compiler_repetitions=args.compiler_repetitions,
        global_mesh_factors=list(args.global_mesh_factors),
        door_mesh_factors=list(args.door_mesh_factors),
        llm_repetitions=args.llm_repetitions,
        modification_repetitions=args.modification_repetitions,
        run_llm=args.run_llm,
        run_abaqus=args.run_abaqus,
        compile_modifications=args.compile_modifications,
        allow_direct_inp_ablation=args.allow_direct_inp_ablation,
        include_internal_engineering=args.include_internal_engineering,
    )
    report = run_experiment_campaign(config, root=args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
