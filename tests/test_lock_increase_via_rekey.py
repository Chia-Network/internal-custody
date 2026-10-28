"""Lock-level increases performed through the standard rekey flow"""

import pytest

from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.util.errors import Err
from chia_rs import SpendBundle
from chia_rs.sized_ints import uint32

from cic.drivers.prefarm import construct_full_singleton
from cic.drivers.puzzle_root_construction import calculate_puzzle_root, derive_next_lock_level

from tests.lock_increase_helpers import (
    finished_singleton_lineage,
    payment_bundle,
    rekey_cancel_bundle,
    rekey_finish_bundle,
    rekey_start_bundle,
    started_singleton,
    started_singleton_lineage,
)
from tests.series_helpers import expected_next, pass_time_and_farm, push_bundle
from tests.timelock_profiles import PROFILES, launch_with_profile

PAYMENT_AMOUNT = 1_000_000
EARLY_DELTA = 10  # boundary margin for block timestamps


def _spends_only_external_coins(bundle: SpendBundle) -> bool:
    spent_ids = {spend.coin.name() for spend in bundle.coin_spends}
    added_ids = {coin.name() for coin in bundle.additions()}
    return spent_ids.isdisjoint(added_ids)


@PROFILES
@pytest.mark.asyncio
async def test_increase_3_to_4(profile):
    async with launch_with_profile(profile) as launched:
        next_derivation = derive_next_lock_level(launched.derivation)
        start_bundle, rekey_coin = rekey_start_bundle(launched, 3, next_derivation)

        await pass_time_and_farm(launched.sim, profile.rekey_increments - EARLY_DELTA)
        result = await push_bundle(launched.sim_client, start_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)

        await pass_time_and_farm(launched.sim, EARLY_DELTA)
        result = await push_bundle(launched.sim_client, start_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        current = started_singleton(launched.singleton)
        current_lineage = started_singleton_lineage(launched.singleton, launched.derivation)
        finish_bundle = rekey_finish_bundle(
            current,
            current_lineage,
            rekey_coin,
            current_lineage,
            3,
            launched.derivation,
            next_derivation,
        )

        await pass_time_and_farm(launched.sim, profile.rekey_clawback_period - EARLY_DELTA)
        result = await push_bundle(launched.sim_client, finish_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)

        await pass_time_and_farm(launched.sim, EARLY_DELTA)
        result = await push_bundle(launched.sim_client, finish_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        new_singleton = next(coin for coin in finish_bundle.additions() if coin.amount == launched.singleton.amount)
        assert new_singleton.puzzle_hash == construct_full_singleton(next_derivation.prefarm_info).get_tree_hash()
        assert next_derivation.prefarm_info.puzzle_root == launched.derivation.next_root
        assert (
            calculate_puzzle_root(
                launched.derivation.prefarm_info,
                launched.derivation.pubkey_list,
                uint32(4),
                uint32(5),
                uint32(1),
            ).prefarm_info.puzzle_root
            == launched.derivation.next_root
        )

        new_lineage = finished_singleton_lineage(launched.singleton, launched.derivation)
        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)

        too_few_payment = payment_bundle(
            new_singleton,
            new_lineage,
            next_derivation,
            PAYMENT_AMOUNT,
            (0, 1, 2),
        )
        result = await push_bundle(launched.sim_client, too_few_payment)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)

        enough_payment = payment_bundle(
            new_singleton,
            new_lineage,
            next_derivation,
            PAYMENT_AMOUNT,
            (0, 1, 2, 3),
        )
        result = await push_bundle(launched.sim_client, enough_payment)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()


@PROFILES
@pytest.mark.asyncio
async def test_increase_4_to_5_then_max(profile):
    async with launch_with_profile(profile) as launched:
        next_derivation = derive_next_lock_level(launched.derivation)
        start_bundle, rekey_coin = rekey_start_bundle(launched, 3, next_derivation)

        await pass_time_and_farm(launched.sim, profile.rekey_increments)
        result = await push_bundle(launched.sim_client, start_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        current = started_singleton(launched.singleton)
        current_lineage = started_singleton_lineage(launched.singleton, launched.derivation)
        finish_bundle = rekey_finish_bundle(
            current,
            current_lineage,
            rekey_coin,
            current_lineage,
            3,
            launched.derivation,
            next_derivation,
        )

        await pass_time_and_farm(launched.sim, profile.rekey_clawback_period)
        result = await push_bundle(launched.sim_client, finish_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        new_singleton = next(coin for coin in finish_bundle.additions() if coin.amount == launched.singleton.amount)
        new_lineage = finished_singleton_lineage(launched.singleton, launched.derivation)

        next_next_derivation = derive_next_lock_level(next_derivation)
        second_start, second_rekey_coin = rekey_start_bundle(
            launched,
            4,
            next_next_derivation,
            singleton=new_singleton,
            lineage=new_lineage,
            derivation=next_derivation,
        )

        await pass_time_and_farm(launched.sim, profile.rekey_increments)
        result = await push_bundle(launched.sim_client, second_start)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        second_current = started_singleton(new_singleton)
        second_lineage = started_singleton_lineage(new_singleton, next_derivation)
        second_finish = rekey_finish_bundle(
            second_current,
            second_lineage,
            second_rekey_coin,
            second_lineage,
            4,
            next_derivation,
            next_next_derivation,
        )

        await pass_time_and_farm(launched.sim, profile.rekey_clawback_period)
        result = await push_bundle(launched.sim_client, second_finish)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        final_singleton = next(coin for coin in second_finish.additions() if coin.amount == launched.singleton.amount)
        assert (
            final_singleton.puzzle_hash == construct_full_singleton(next_next_derivation.prefarm_info).get_tree_hash()
        )
        with pytest.raises(ValueError, match="already at maximum lock level"):
            derive_next_lock_level(next_next_derivation)


@PROFILES
@pytest.mark.asyncio
async def test_increase_bundles_are_single_push(profile):
    async with launch_with_profile(profile) as launched:
        next_derivation = derive_next_lock_level(launched.derivation)
        start_bundle, rekey_coin = rekey_start_bundle(launched, 3, next_derivation)
        assert _spends_only_external_coins(start_bundle)

        await pass_time_and_farm(launched.sim, profile.rekey_increments)
        result = await push_bundle(launched.sim_client, start_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        restore_height = launched.sim.block_height
        current = started_singleton(launched.singleton)
        current_lineage = started_singleton_lineage(launched.singleton, launched.derivation)
        finish_bundle = rekey_finish_bundle(
            current,
            current_lineage,
            rekey_coin,
            current_lineage,
            3,
            launched.derivation,
            next_derivation,
        )
        assert _spends_only_external_coins(finish_bundle)

        await pass_time_and_farm(launched.sim, profile.rekey_clawback_period)
        result = await push_bundle(launched.sim_client, finish_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        await launched.sim.rewind(restore_height)
        cancel_bundle = rekey_cancel_bundle(rekey_coin, (0, 1, 2), launched.derivation, next_derivation)
        assert _spends_only_external_coins(cancel_bundle)
        result = await push_bundle(launched.sim_client, cancel_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()


@PROFILES
@pytest.mark.asyncio
async def test_increase_can_be_cancelled(profile):
    async with launch_with_profile(profile) as launched:
        next_derivation = derive_next_lock_level(launched.derivation)
        start_bundle, rekey_coin = rekey_start_bundle(launched, 3, next_derivation)

        await pass_time_and_farm(launched.sim, profile.rekey_increments)
        result = await push_bundle(launched.sim_client, start_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        one_key_cancel = rekey_cancel_bundle(rekey_coin, (0,), launched.derivation, next_derivation)
        result = await push_bundle(launched.sim_client, one_key_cancel)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)

        two_key_cancel = rekey_cancel_bundle(rekey_coin, (0, 1), launched.derivation, next_derivation)
        result = await push_bundle(launched.sim_client, two_key_cancel)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)

        three_key_cancel = rekey_cancel_bundle(rekey_coin, (0, 1, 2), launched.derivation, next_derivation)
        result = await push_bundle(launched.sim_client, three_key_cancel)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        current = started_singleton(launched.singleton)
        assert current.puzzle_hash == construct_full_singleton(launched.derivation.prefarm_info).get_tree_hash()

        current_lineage = started_singleton_lineage(launched.singleton, launched.derivation)
        finish_bundle = rekey_finish_bundle(
            current,
            current_lineage,
            rekey_coin,
            current_lineage,
            3,
            launched.derivation,
            next_derivation,
        )
        result = await push_bundle(launched.sim_client, finish_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.DOUBLE_SPEND)

        rekey_coin_record = await launched.sim_client.get_coin_record_by_name(rekey_coin.name())
        assert rekey_coin_record is not None
        assert rekey_coin_record.spent


@PROFILES
@pytest.mark.asyncio
async def test_payments_during_increase(profile):
    async with launch_with_profile(profile) as launched:
        next_derivation = derive_next_lock_level(launched.derivation)
        start_bundle, rekey_coin = rekey_start_bundle(launched, 3, next_derivation)

        await pass_time_and_farm(launched.sim, profile.rekey_increments)
        result = await push_bundle(launched.sim_client, start_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        current = started_singleton(launched.singleton)
        payment = payment_bundle(
            current,
            started_singleton_lineage(launched.singleton, launched.derivation),
            launched.derivation,
            PAYMENT_AMOUNT,
            (0, 1, 2),
        )
        result = await push_bundle(launched.sim_client, payment)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)

        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)
        result = await push_bundle(launched.sim_client, payment)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        post_payment, post_payment_lineage = expected_next(
            current,
            PAYMENT_AMOUNT,
            0,
            launched.derivation,
        )
        rekey_lineage = started_singleton_lineage(launched.singleton, launched.derivation)
        finish_bundle = rekey_finish_bundle(
            post_payment,
            post_payment_lineage,
            rekey_coin,
            rekey_lineage,
            3,
            launched.derivation,
            next_derivation,
        )

        remaining = profile.rekey_clawback_period - profile.withdrawal_timelock
        await pass_time_and_farm(launched.sim, remaining)
        result = await push_bundle(launched.sim_client, finish_bundle)
        assert result == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()
