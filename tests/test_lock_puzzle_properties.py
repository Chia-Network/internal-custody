"""Tests of the driver-level lock puzzle spends"""

import dataclasses

from typing import List

import pytest
import pytest_asyncio

from chia_rs import AugSchemeMPL, G1Element, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32
from chia.types.blockchain_format.coin import Coin
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.coin_spend import CoinSpend
from chia.util.errors import Err
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.drop_coins import solve_rekey_clawback
from cic.drivers.prefarm import (
    calculate_rekey_args,
    construct_full_singleton,
    construct_singleton_inner_puzzle,
    get_rekey_spend_info,
)
from cic.drivers.puzzle_root_construction import calculate_puzzle_root, derive_next_lock_level
from tests import run_coin_spend
from tests.test_lifecycle import secret_key_for_index, sign_message_at_index
from tests.timelock_profiles import SHORT_TIMELOCKS, launch_with_profile


def aggregate_pubkeys(pubkeys: List[G1Element]) -> G1Element:
    """Return the aggregate of the given public keys."""
    aggregate = G1Element()
    for pubkey in pubkeys:
        aggregate += pubkey
    return aggregate


@pytest_asyncio.fixture(scope="function")
async def _setup_info():
    """Launch a singleton with the short timelock settings."""
    async with launch_with_profile(SHORT_TIMELOCKS) as launched:
        yield launched


@pytest.mark.asyncio
async def test_one_shot_lock_bundle_rejected(_setup_info):
    """chia-blockchain 2.7.5 rejects relative time conditions on coins created and spent in the same block."""
    setup_info = _setup_info
    four_pubkeys = [secret_key_for_index(i).get_g1() for i in range(0, 4)]
    aggregate_four = aggregate_pubkeys(four_pubkeys)

    lock_bundle, data_to_sign = get_rekey_spend_info(
        setup_info.singleton,
        four_pubkeys,
        setup_info.derivation,
        setup_info.lineage,
    )
    signature: G2Element = AugSchemeMPL.aggregate(
        [sign_message_at_index(i, data_to_sign, aggregate_four) for i in range(0, 4)]
    )
    aggregate_bundle = SpendBundle.aggregate([lock_bundle, SpendBundle([], signature)])

    result = await setup_info.sim_client.push_tx(aggregate_bundle)
    assert result == (MempoolInclusionStatus.FAILED, Err.EPHEMERAL_RELATIVE_CONDITION)

    next_derivation = calculate_puzzle_root(
        setup_info.derivation.prefarm_info,
        setup_info.derivation.pubkey_list,
        uint32(4),
        uint32(5),
        uint32(1),
    )
    assert next_derivation.prefarm_info.puzzle_root == setup_info.derivation.next_root


@pytest.mark.asyncio
async def test_lock_completion_coin_accepts_only_completion_solution(_setup_info):
    """The p2_new_lock_level coin only accepts its completion solution."""
    setup_info = _setup_info
    four_pubkeys = [secret_key_for_index(i).get_g1() for i in range(0, 4)]
    aggregate_four = aggregate_pubkeys(four_pubkeys)

    lock_bundle, data_to_sign = get_rekey_spend_info(
        setup_info.singleton,
        four_pubkeys,
        setup_info.derivation,
        setup_info.lineage,
    )
    signature: G2Element = AugSchemeMPL.aggregate(
        [sign_message_at_index(i, data_to_sign, aggregate_four) for i in range(0, 4)]
    )
    aggregate_bundle = SpendBundle.aggregate([lock_bundle, SpendBundle([], signature)])

    lock_completion_spend: CoinSpend = [
        cs
        for cs in aggregate_bundle.coin_spends
        if cs.coin.parent_coin_info == setup_info.singleton.name() and cs.coin.amount == 0
    ][0]
    singleton_spend: CoinSpend = [cs for cs in aggregate_bundle.coin_spends if cs.coin == setup_info.singleton][0]
    _, new_puzzle_root, filter_puzzle, filter_proof, filter_solution, _ = calculate_rekey_args(
        lock_completion_spend.coin,
        four_pubkeys,
        setup_info.derivation,
    )
    cancellation_spend: CoinSpend = lock_completion_spend.replace(
        solution=solve_rekey_clawback(
            setup_info.derivation.prefarm_info,
            new_puzzle_root,
            filter_puzzle,
            filter_proof,
            filter_solution,
        ).to_serialized(),
    )
    extra_signature: G2Element = AugSchemeMPL.aggregate(
        [sign_message_at_index(i, data_to_sign, aggregate_four) for i in range(0, 4)],
    )
    ephemeral_cancel_bundle = SpendBundle.aggregate(
        [SpendBundle([singleton_spend, cancellation_spend], signature), SpendBundle([], extra_signature)]
    )

    result = await setup_info.sim_client.push_tx(ephemeral_cancel_bundle)
    assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
    with pytest.raises(ValueError, match="clvm raise"):
        run_coin_spend(cancellation_spend)


@pytest.mark.asyncio
async def test_lock_leaf_requires_one_more_key(_setup_info):
    """The 4-of-5 derivation has no leaf for the four-key aggregate."""
    setup_info = _setup_info
    four_pubkeys = [secret_key_for_index(i).get_g1() for i in range(0, 4)]

    new_derivation = calculate_puzzle_root(
        setup_info.derivation.prefarm_info,
        setup_info.derivation.pubkey_list,
        uint32(4),
        uint32(5),
        uint32(1),
    )
    new_singleton = Coin(
        setup_info.singleton.name(),
        construct_full_singleton(new_derivation.prefarm_info).get_tree_hash(),
        setup_info.singleton.amount,
    )
    new_lineage_proof = LineageProof(
        setup_info.singleton.parent_coin_info,
        construct_singleton_inner_puzzle(setup_info.derivation.prefarm_info).get_tree_hash(),
        setup_info.singleton.amount,
    )

    with pytest.raises(ValueError, match="Could not find a puzzle matching the specified pubkey"):
        get_rekey_spend_info(
            new_singleton,
            four_pubkeys,
            new_derivation,
            new_lineage_proof,
        )


@pytest.mark.asyncio
async def test_derive_next_lock_level_rejects_wrong_next_root(_setup_info):
    """The derived root must match the configured next root."""
    derivation = dataclasses.replace(_setup_info.derivation, next_root=bytes32(b"\x00" * 32))

    with pytest.raises(ValueError, match="does not match the configured next root"):
        derive_next_lock_level(derivation)


@pytest.mark.asyncio
async def test_derive_next_lock_level_rejects_maximum_level(_setup_info):
    """A derivation without a next root is already at the maximum lock level."""
    derivation = dataclasses.replace(_setup_info.derivation, next_root=None)

    with pytest.raises(ValueError, match="already at maximum lock level"):
        derive_next_lock_level(derivation)
