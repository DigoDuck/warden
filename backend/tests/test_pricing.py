"""Cost feeds a published metrics table, so it is arithmetic that has to be checked."""

from decimal import Decimal

import pytest

from warden.providers.base import Usage
from warden.providers.pricing import UnknownModelError, cost_usd


def test_known_model_costs_the_posted_rate() -> None:
    # claude-opus-5 is $5.00 per 1M input and $25.00 per 1M output.
    # 200_000 in  -> 0.2 * 5.00  = 1.00
    # 100_000 out -> 0.1 * 25.00 = 2.50
    usage = Usage(input_tokens=200_000, output_tokens=100_000)
    assert cost_usd("claude-opus-5", usage) == Decimal("3.500000")


def test_cheaper_model_costs_less_for_identical_usage() -> None:
    usage = Usage(input_tokens=200_000, output_tokens=100_000)
    assert cost_usd("claude-haiku-4-5", usage) < cost_usd("claude-opus-5", usage)


def test_cache_read_is_billed_below_the_input_rate() -> None:
    """A cached token must not be billed as a fresh input token."""
    fresh = Usage(input_tokens=1_000_000)
    cached = Usage(cache_read_input_tokens=1_000_000)
    assert cost_usd("claude-opus-5", fresh) == Decimal("5.000000")
    assert cost_usd("claude-opus-5", cached) == Decimal("0.500000")


def test_cache_write_is_billed_above_the_input_rate() -> None:
    written = Usage(cache_creation_input_tokens=1_000_000)
    assert cost_usd("claude-opus-5", written) == Decimal("6.250000")


def test_result_is_quantised_to_the_database_column() -> None:
    """`model_calls.cost_usd` is Numeric(12, 6); what is asserted must be what is stored."""
    cost = cost_usd("claude-opus-5", Usage(input_tokens=1, output_tokens=1))
    assert cost.as_tuple().exponent == -6


def test_zero_usage_costs_nothing() -> None:
    assert cost_usd("claude-opus-5", Usage()) == Decimal("0")


def test_unknown_model_raises_and_names_it() -> None:
    """Never a silent zero: that would become a wrong number in docs/metrics.md."""
    with pytest.raises(UnknownModelError) as excinfo:
        cost_usd("gpt-9-ultra", Usage(input_tokens=10))
    assert "gpt-9-ultra" in str(excinfo.value)
    assert "claude-opus-5" in str(excinfo.value)
