"""Persistence-mechanism + storage-mode + protection -> IOC difficulty.

Sole source of truth for the auto-difficulty formula.  Exported via
`GET /c2/difficulty-formula` so the Generate-page React helper reads the same
weights (`react/src/lib/difficultyFormula.ts`).

See `docs/PLAN_generate_page_labels_autodifficulty.md` §5 for the weight
table + rationale.
"""
from __future__ import annotations

from dataclasses import dataclass

STORAGE_MODE_WEIGHTS: dict[str, float] = {
    "file": 0.0,
    "self_inject": 0.5,
    "self_inject_persist": 1.0,
    "registry": 1.0,
    "pe_section": 1.0,
    "xattr": 0.5,
    "elf_section": 0.5,
    "extattr": 0.5,
}

PROTECTION_WEIGHTS: dict[str, float] = {
    "enable_hiding": 1.5,
    "apply_immutable_binary_only": 0.5,
    "apply_immutable_binary_and_persistence": 0.75,
    "timestomp": 0.25,
    "backup_copy_enabled": 0.5,
}

BUCKET_BOUNDARIES: list[tuple[float, int]] = [
    (1.5, 1),  # Easy
    (2.5, 2),  # Medium
    (3.5, 3),  # Hard
    (float("inf"), 4),  # Ultra
]


@dataclass(frozen=True)
class DifficultyInputs:
    """One tuple of Generate-page selections that feed the difficulty formula."""

    mechanism_tier: int   # 1..3 from PersistenceMechanism.tier
    storage_mode: str
    apply_immutable: bool
    immutable_scope: str  # "binary_only" | "binary_and_persistence"
    timestomp: bool
    backup_copy_enabled: bool
    enable_hiding: bool


def compute_difficulty(inputs: DifficultyInputs) -> int:
    """Return the bucketed difficulty (1..4) for the given Generate-page selections."""
    score = float(inputs.mechanism_tier)
    score += STORAGE_MODE_WEIGHTS.get(inputs.storage_mode, 0.0)

    if inputs.enable_hiding:
        score += PROTECTION_WEIGHTS["enable_hiding"]
    if inputs.apply_immutable:
        key = f"apply_immutable_{inputs.immutable_scope}"
        score += PROTECTION_WEIGHTS.get(key, 0.0)
    if inputs.timestomp:
        score += PROTECTION_WEIGHTS["timestomp"]
    # backup_copy is a no-op under self-inject storage modes — the deploy path
    # skips file staging entirely when there's no on-disk implant to copy.
    if inputs.backup_copy_enabled and not inputs.storage_mode.startswith("self_inject"):
        score += PROTECTION_WEIGHTS["backup_copy_enabled"]

    for boundary, bucket in BUCKET_BOUNDARIES:
        if score <= boundary:
            return bucket
    return 4  # unreachable but explicit
