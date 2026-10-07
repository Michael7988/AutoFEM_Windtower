from __future__ import annotations

# Compatibility facade.  The versioned profile intentionally lives above the
# modeling package so parameter IR, adapters, L0 and experiment code can share
# it without creating an import cycle.
from ..capability_profile import (
    CAPABILITY_HASH_SHA256,
    CAPABILITY_ID,
    CAPABILITY_REVISION,
    L2_CAPABILITY_HASH_SHA256,
    L2_CAPABILITY_ID,
    capability_exists,
    current_capability,
    default_fem_parameters,
    get_capability,
    input_format_registry,
    list_capabilities,
)

__all__ = [
    "CAPABILITY_HASH_SHA256",
    "CAPABILITY_ID",
    "CAPABILITY_REVISION",
    "L2_CAPABILITY_HASH_SHA256",
    "L2_CAPABILITY_ID",
    "capability_exists",
    "current_capability",
    "default_fem_parameters",
    "get_capability",
    "input_format_registry",
    "list_capabilities",
]
