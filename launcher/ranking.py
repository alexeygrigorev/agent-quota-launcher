"""Bounded auditable ranking per docs/research.md:

    weight = fit * health * preference * headroom * (1 + 2 * urgency)
    headroom = max(0.05, bottleneck_remaining)
    urgency  = remaining * clamp((24 - hours_to_reset) / 24, 0, 1)

The reset multiplier lies in [1, 3]; missing or unknown reset adds no urgency.
Unknown health/task_fit weight the route at 0 (never selected, never
fabricated to 1.0). Seed, weights and probabilities are recorded for audit.
"""
import math
import random

PREFERENCE_PROVIDERS = ("grok", "zai")
PREFERENCE_FACTOR = 1.2
PREFERENCE_MAX_HOURS = 24.0
HEADROOM_FLOOR = 0.05


def reset_multiplier(remaining_fraction, hours_to_reset):
    if remaining_fraction is None or hours_to_reset is None:
        return 1.0
    if not math.isfinite(remaining_fraction) or not math.isfinite(hours_to_reset):
        return 1.0
    clamp = max(0.0, min(1.0, (24.0 - hours_to_reset) / 24.0))
    urgency = remaining_fraction * clamp
    return max(1.0, min(3.0, 1.0 + 2.0 * urgency))


def calculate_weight(task_fit, health, remaining_fraction, hours_to_reset, promo_multiplier,
                     provider=None):
    """Returns (weight, reason). weight 0.0 means not selectable; the reason
    says whether that is unknown evidence, exhaustion or a computed 0."""
    if task_fit is None or health is None:
        return 0.0, "unknown task_fit/health (no fabricated 1.0)"
    if remaining_fraction is None or not math.isfinite(remaining_fraction) or remaining_fraction <= 0:
        return 0.0, "no known remaining quota"
    if task_fit <= 0 or health <= 0:
        return 0.0, "task fit or health is zero"
    if promo_multiplier is None:
        promo_multiplier = 1.0

    headroom = max(HEADROOM_FLOOR, remaining_fraction)
    mult = reset_multiplier(remaining_fraction, hours_to_reset)
    pref_applies = (
        provider in PREFERENCE_PROVIDERS
        and hours_to_reset is not None
        and math.isfinite(hours_to_reset)
        and hours_to_reset <= PREFERENCE_MAX_HOURS
    )
    pref = PREFERENCE_FACTOR if pref_applies else 1.0
    weight = task_fit * health * pref * headroom * mult * promo_multiplier
    if not math.isfinite(weight) or weight <= 0:
        return 0.0, "computed nonpositive/nonfinite weight"
    return weight, "computed"


def select_candidate(candidates, seed=None):
    """Weighted choice among selectable candidates using random.Random(seed).

    Returns (chosen, provenance). provenance records seed, rng, per-candidate
    weight/probability/reason and the chosen name; randomness never makes an
    ineligible route eligible.
    """
    rng = random.Random(seed)
    entries = []
    for c in candidates:
        weight, reason = calculate_weight(
            c.get("task_fit"),
            c.get("health"),
            c.get("remaining_fraction"),
            c.get("hours_to_reset"),
            c.get("promo_multiplier", 1.0),
            provider=c.get("provider"),
        )
        entries.append({
            "name": c.get("name"),
            "provider": c.get("provider"),
            "weight": weight,
            "probability": None,
            "reason": reason,
        })

    selectable = [e for e in entries if e["weight"] > 0]
    total = sum(e["weight"] for e in selectable)
    if total > 0:
        for e in selectable:
            e["probability"] = e["weight"] / total

    provenance = {
        "seed": seed,
        "rng": "random.Random(seed).choices over weight>0 candidates",
        "candidates": entries,
        "chosen": None,
    }
    if not selectable:
        return None, provenance

    chosen = rng.choices(
        [e["name"] for e in selectable],
        weights=[e["weight"] for e in selectable],
        k=1,
    )[0]
    provenance["chosen"] = chosen
    for c in candidates:
        if c.get("name") == chosen:
            return c, provenance
    return None, provenance
