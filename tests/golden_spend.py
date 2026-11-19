"""Deterministic unsigned spend used by the golden file compatibility tests."""

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_offset,
)
from chia_rs import PrivateKey
from chia_base.atoms import bytes32 as HSMbytes32
from chia_base.atoms import uint64 as HSMuint64
from chia_base.bls12_381 import BLSPublicKey, BLSSecretExponent
from chia_base.core import Coin as HSMCoin
from chia_base.core import CoinSpend as HSMCoinSpend
from clvm_rs import Program

from hsms.core.signing_hints import SumHint
from hsms.core.unsigned_spend import UnsignedSpend

from tests.cli_helpers import secret_key_for_index

ACS = Program.to(1)


def build_golden_unsigned_spend() -> UnsignedSpend:
    """Rebuild the deterministic spend stored in the golden file."""
    puzzle_hash = HSMbytes32(bytes(ACS.tree_hash()))
    coin_spends = [
        HSMCoinSpend(
            HSMCoin(HSMbytes32(bytes([1] * 32)), puzzle_hash, HSMuint64(1)),
            ACS,
            Program.to([]),
        ),
        HSMCoinSpend(
            HSMCoin(HSMbytes32(bytes([2] * 32)), puzzle_hash, HSMuint64(2)),
            ACS,
            Program.to([]),
        ),
    ]
    public_keys = [BLSPublicKey(secret_key_for_index(index).get_g1()) for index in range(3)]
    aggregate_key = sum(public_keys, start=BLSPublicKey.zero())
    synthetic_secret = BLSSecretExponent(
        PrivateKey.from_bytes(
            calculate_synthetic_offset(aggregate_key._g1, DEFAULT_HIDDEN_PUZZLE_HASH).to_bytes(32, "big")
        )
    )
    return UnsignedSpend(
        coin_spends,
        [SumHint(public_keys, synthetic_secret)],
        [],
        DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA,
    )
