"""L0 contract normalization, derivation and traceability exports.

The implementation remains byte-compatible with the validated compiler while
the public surface is grouped by responsibility.
"""

from .implementation import (
    build_derived_geometry,
    build_load_contract,
    build_model_hash,
    build_source_trace,
    enrich_l0_ir,
    summarize_load_readiness,
)

__all__ = [
    "build_derived_geometry",
    "build_load_contract",
    "build_model_hash",
    "build_source_trace",
    "enrich_l0_ir",
    "summarize_load_readiness",
]
