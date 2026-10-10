"""USD pricing helpers for the dollar ledger (integer micro-dollars, no floats).

All money in the budget ledger is an ``int`` number of micro-dollars (``usd_micros``):
1 USD == 1_000_000 usd_micros. Floats are rejected on purpose: they are the source of drift.

Cost sources, in order of preference:
1. The provider's own estimate: Higgsfield ``POST /estimate/{model}`` returns
   ``{"credits": "<str>", "usd": "<str>"}``; the ``usd`` string is converted exactly (Decimal),
   rounding UP to the next micro-dollar (never under-reserve).
2. For models whose estimate carries no USD value (a pricing *description*, e.g. Seedance 2.5),
   a configurable per-second USD rate table (``config.MODEL_USD_PER_SECOND_MICROS``).
   Those rates are LIST PRICE BEFORE DISCOUNT and are marked ``derived`` in the result.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from hfvg.config import config

MICROS_PER_USD = 1_000_000

# Fake (dry-run) reservation sizes in usd_micros: nothing is reserved in dry mode
DRY_STILL_USD_MICROS = 60_000  # 0.06 USD
DRY_CLIP_USD_PER_SECOND_MICROS = 56_000  # 0.056 USD/s


def usd_to_micros(value: Any) -> int:
    """Convert a USD amount given as str/int/Decimal to integer micro-dollars (rounds up).

    Floats are refused (TypeError): pass the provider's decimal string instead.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"USD amounts must be str/int/Decimal, not {type(value).__name__}")
    try:
        dec = Decimal(value) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ValueError(f"Invalid USD amount: {value!r}") from e
    if not dec.is_finite() or dec < 0:
        raise ValueError(f"USD amount must be a finite, non-negative number: {value!r}")
    return int((dec * MICROS_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def micros_to_usd_str(micros: int) -> str:
    """Format integer micro-dollars as a USD string with at least 2 decimals (exact)."""
    if isinstance(micros, bool) or not isinstance(micros, int):
        raise TypeError(f"usd_micros must be int, got {type(micros).__name__}")
    sign = "-" if micros < 0 else ""
    whole, frac = divmod(abs(micros), MICROS_PER_USD)
    frac_s = f"{frac:06d}".rstrip("0").ljust(2, "0")
    return f"{sign}{whole}.{frac_s}"


@dataclass(frozen=True)
class UsdEstimate:
    usd_micros: int
    source: str  # "provider_estimate" | "rate_table"
    derived: bool
    note: str = ""


def estimate_from_response(data: dict[str, Any]) -> int | None:
    """Return usd_micros from a Higgsfield estimate response, or None if it has no USD value."""
    usd = data.get("usd") if isinstance(data, dict) else None
    if usd is None:
        return None
    if not isinstance(usd, str):
        raise ValueError(f"Estimate 'usd' must be a string, got {type(usd).__name__}")
    return usd_to_micros(usd)


def rate_table_estimate(model_key: str, resolution: str | None, seconds: int | float) -> UsdEstimate:
    """Cost from the configured per-second USD rate table (list price before discount)."""
    table = config.MODEL_USD_PER_SECOND_MICROS
    model_rates = table.get(model_key)
    if not model_rates:
        raise ValueError(f"No USD estimate and no configured USD rate for model {model_key!r}")
    key = resolution if resolution in model_rates else "default"
    if key not in model_rates:
        raise ValueError(f"No configured USD rate for {model_key!r} at resolution {resolution!r}")
    per_second = model_rates[key]
    if isinstance(per_second, bool) or not isinstance(per_second, int):
        raise TypeError("Configured USD rates must be integer micro-dollars per second")
    # Decimal seconds avoid float multiplication; round up
    cost = int((Decimal(str(seconds)) * per_second).to_integral_value(rounding=ROUND_CEILING))
    return UsdEstimate(
        usd_micros=cost,
        source="rate_table",
        derived=True,
        note=f"{model_key}@{key}: {per_second} usd_micros/s x {seconds}s (list price before discount)",
    )
