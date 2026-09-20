"""Cost of a model call, in dollars.

Decimal all the way through, matching `model_calls.cost_usd` being `Numeric(12, 6)` in the
schema. These numbers get summed per task and per experiment and published in
docs/metrics.md, so accumulated floating point error there would be a published lie.
"""

from dataclasses import dataclass
from decimal import Decimal

from warden.providers.base import Usage

_PER_MTOK = Decimal(1_000_000)

# Cache tokens are not billed at the input rate. These multipliers are the documented
# approximations, not contract: the authoritative source is
# https://www.anthropic.com/pricing. Revisit when a published metrics table depends on
# cache-heavy runs.
CACHE_WRITE_MULTIPLIER = Decimal("1.25")
CACHE_READ_MULTIPLIER = Decimal("0.1")

# Quantum of the `Numeric(12, 6)` column. Rounding here rather than at insert time keeps
# what the tests assert and what the database stores identical.
_CENTS = Decimal("0.000001")


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


# Only the models this project actually routes between: one frontier and two cheaper ones,
# which is what the routing strategies in briefing section 18 need. Listing the whole
# catalogue would be writing rows nobody reads.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": ModelPrice(Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-4-5": ModelPrice(Decimal("1.00"), Decimal("5.00")),
    # The FakeProvider's model. Priced explicitly at zero rather than special-cased inside
    # cost_usd, so a scripted run goes down the same arithmetic as a real one and an
    # unpriced model still raises.
    "fake-model": ModelPrice(Decimal("0"), Decimal("0")),
}


class UnknownModelError(LookupError):
    """Raised instead of returning zero for a model that has no price.

    A silent zero would flow straight into the published cost table. Failing loudly at the
    first call is cheaper than discovering the numbers were wrong after publishing them.
    """


def cost_usd(model: str, usage: Usage) -> Decimal:
    try:
        price = PRICES[model]
    except KeyError:
        known = ", ".join(sorted(PRICES))
        raise UnknownModelError(
            f"no price for model {model!r}; known models: {known}. "
            f"Add it to warden.providers.pricing.PRICES."
        ) from None

    billable_input = (
        Decimal(usage.input_tokens)
        + Decimal(usage.cache_creation_input_tokens) * CACHE_WRITE_MULTIPLIER
        + Decimal(usage.cache_read_input_tokens) * CACHE_READ_MULTIPLIER
    )
    total = (
        billable_input * price.input_per_mtok + Decimal(usage.output_tokens) * price.output_per_mtok
    ) / _PER_MTOK
    return total.quantize(_CENTS)
