"""Helpers for tests of lock-level increases through the rekey flow"""

from typing import Optional, Sequence, Tuple

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.wallet.lineage_proof import LineageProof
from chia_rs import AugSchemeMPL, G1Element, SpendBundle
from chia_rs.sized_ints import uint8, uint64

from cic.drivers.prefarm import (
    construct_singleton_inner_puzzle,
    get_rekey_clawback_spend_info,
    get_rekey_completion_spend,
    get_rekey_spend_info,
    get_withdrawal_spend_info,
)
from cic.drivers.puzzle_root_construction import RootDerivation

from tests.test_lifecycle import (
    ACS_PH,
    get_synthetic_pubkey,
    secret_key_for_index,
    sign_message_at_index,
    sign_message_with_offset,
)
from tests.timelock_profiles import LaunchedWithProfile, sign_payment


def sign_rekey(bundle: SpendBundle, data_to_sign: bytes, key_indices: Sequence[int]) -> SpendBundle:
    """Sign a rekey bundle with the keys at the given indices."""
    pubkeys: list[G1Element] = [secret_key_for_index(index).get_g1() for index in key_indices]
    aggregate_pubkey = G1Element()
    for pubkey in pubkeys:
        aggregate_pubkey += pubkey
    synthetic_pubkey = get_synthetic_pubkey(aggregate_pubkey)
    signature = AugSchemeMPL.aggregate(
        [
            sign_message_with_offset(aggregate_pubkey, data_to_sign, synthetic_pubkey),
            *[sign_message_at_index(index, data_to_sign, synthetic_pubkey) for index in key_indices],
        ]
    )
    return SpendBundle.aggregate([bundle, SpendBundle([], signature)])


def rekey_start_bundle(
    launched: LaunchedWithProfile,
    key_count: int,
    next_derivation: RootDerivation,
    singleton: Optional[Coin] = None,
    lineage: Optional[LineageProof] = None,
    derivation: Optional[RootDerivation] = None,
) -> Tuple[SpendBundle, Coin]:
    """Build and sign the START bundle for a lock-level increase."""
    coin = launched.singleton if singleton is None else singleton
    coin_lineage = launched.lineage if lineage is None else lineage
    current_derivation = launched.derivation if derivation is None else derivation
    pubkeys = [secret_key_for_index(index).get_g1() for index in range(key_count)]
    bundle, data_to_sign = get_rekey_spend_info(
        coin,
        pubkeys,
        current_derivation,
        coin_lineage,
        next_derivation,
        [Program.to([60, b""])],
    )
    signed_bundle = sign_rekey(bundle, data_to_sign, tuple(range(key_count)))
    rekey_coin = next(coin for coin in signed_bundle.additions() if coin.amount == 0)
    return signed_bundle, rekey_coin


def rekey_finish_bundle(
    singleton: Coin,
    singleton_lineage: LineageProof,
    rekey_coin: Coin,
    rekey_lineage: LineageProof,
    key_count: int,
    derivation: RootDerivation,
    new_derivation: RootDerivation,
) -> SpendBundle:
    """Build the FINISH bundle for a lock-level increase."""
    pubkeys = [secret_key_for_index(index).get_g1() for index in range(key_count)]
    return get_rekey_completion_spend(
        singleton,
        rekey_coin,
        pubkeys,
        derivation,
        singleton_lineage,
        rekey_lineage,
        new_derivation,
    )


def rekey_cancel_bundle(
    rekey_coin: Coin,
    key_indices: Sequence[int],
    derivation: RootDerivation,
    new_derivation: RootDerivation,
    timelock_multiple: uint8 = uint8(1),
) -> SpendBundle:
    """Build and sign the cancel bundle for a lock-level increase."""
    pubkeys = [secret_key_for_index(index).get_g1() for index in key_indices]
    bundle, data_to_sign = get_rekey_clawback_spend_info(
        rekey_coin,
        pubkeys,
        derivation,
        timelock_multiple,
        new_derivation,
    )
    return sign_rekey(bundle, data_to_sign, key_indices)


def payment_bundle(
    singleton: Coin,
    lineage: LineageProof,
    derivation: RootDerivation,
    amount: int,
    key_indices: Sequence[int],
) -> SpendBundle:
    """Build and sign a payment spend with the given key indices."""
    pubkeys = [secret_key_for_index(index).get_g1() for index in key_indices]
    bundle, data_to_sign = get_withdrawal_spend_info(
        singleton,
        pubkeys,
        derivation,
        lineage,
        uint64(amount),
        ACS_PH,
        p2_singletons_to_claim=[],
    )
    return sign_payment(bundle, data_to_sign, tuple(key_indices))


def started_singleton(singleton: Coin) -> Coin:
    """Return the singleton coin that a START spend creates."""
    return Coin(singleton.name(), singleton.puzzle_hash, singleton.amount)


def started_singleton_lineage(singleton: Coin, derivation: RootDerivation) -> LineageProof:
    """Return the lineage proof for the coins that a START spend creates."""
    return LineageProof(
        singleton.parent_coin_info,
        construct_singleton_inner_puzzle(derivation.prefarm_info).get_tree_hash(),
        singleton.amount,
    )


def finished_singleton_lineage(singleton: Coin, derivation: RootDerivation) -> LineageProof:
    """Return the lineage proof for the singleton coin that a FINISH spend creates."""
    return LineageProof(
        singleton.name(),
        construct_singleton_inner_puzzle(derivation.prefarm_info).get_tree_hash(),
        singleton.amount,
    )
