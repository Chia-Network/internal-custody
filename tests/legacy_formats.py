"""Helpers for working with older serialization formats."""

from typing import List

from clvm_rs import Program

from hsms.clvm_serde import to_program_for_type
from hsms.core.signing_hints import SumHint
from hsms.core.unsigned_spend import SerdeCoinSpends, UnsignedSpend, from_storage


def encode_unsigned_spend_hsms01(spend: UnsignedSpend) -> bytes:
    """Encoding written by hsms 0.1.x, for compatibility tests."""
    coin_spends = to_program_for_type(SerdeCoinSpends)(from_storage(spend.coin_spends))
    sum_hints = to_program_for_type(List[SumHint])(spend.sum_hints)
    program = Program.to(
        [
            ("a", spend.agg_sig_me_network_suffix),
            ("c", coin_spends),
            ("s", sum_hints),
            ("p", Program.to([])),
        ]
    )
    return bytes(program)
