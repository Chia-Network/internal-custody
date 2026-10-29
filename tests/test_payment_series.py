"""Tests for building and checking series of payment spends."""

import re

from pathlib import Path
from typing import List

import pytest

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.util.errors import Err
from chia_rs import G1Element, G2Element, SpendBundle
from chia_rs.sized_ints import uint8, uint32, uint64

from cic.cli.payment_series import advance_through_previous_payments, load_previous_spend
from cic.drivers.drop_coins import curry_ach_puzzle
from cic.drivers.prefarm import (
    get_ach_clawback_spend_info,
    get_rekey_clawback_spend_info,
    get_rekey_spend_info,
)
from cic.drivers.puzzle_root_construction import calculate_puzzle_root
from cic.drivers.singleton import construct_p2_singleton

from tests.series_helpers import (
    bundle_to_unsigned_spend_file,
    create_p2_singleton_coins,
    expected_next,
    make_payment,
    pass_time_and_farm,
    push_bundle,
)
from tests.test_lifecycle import ACS_PH, secret_key_for_index
from tests.timelock_profiles import (
    LONG_TIMELOCKS,
    PROFILES,
    SHORT_TIMELOCKS,
    TimelockProfile,
    launch_with_profile,
    sign_payment,
)


def _pubkeys(indices: range) -> List[G1Element]:
    """Return the public keys for the given key indices."""
    return [secret_key_for_index(index).get_g1() for index in indices]


@pytest.mark.asyncio
async def test_helper_accepts_hsms_types(tmp_path: Path) -> None:
    """Regression test for reading spend files written by hsms 0.3.2."""
    profile = SHORT_TIMELOCKS
    async with launch_with_profile(profile) as launched:
        bundle, _ = make_payment(launched.singleton, launched.lineage, launched.derivation, 10)
        path = str(tmp_path / "p1.spend")
        bundle_to_unsigned_spend_file(path, bundle)
        coin_spends = load_previous_spend(path)
        assert [coin_spend.coin.name() for coin_spend in coin_spends] == [
            coin_spend.coin.name() for coin_spend in bundle.coin_spends
        ]
        coin, lineage, absorbed = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", coin_spends)],
        )
        expected_coin, expected_lineage = expected_next(launched.singleton, 10, 0, launched.derivation)
    assert coin == expected_coin
    assert lineage == expected_lineage
    assert absorbed == set()


@pytest.mark.asyncio
@PROFILES
async def test_series_of_three_payments(profile: TimelockProfile) -> None:
    """Three payments become valid one after another as the timelock passes."""
    async with launch_with_profile(profile) as launched:
        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)
        amounts = (12, 20, 30)

        p1, data1 = make_payment(launched.singleton, launched.lineage, launched.derivation, amounts[0])
        signed1 = sign_payment(p1, data1)
        coin1, lineage1, absorbed1 = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", p1.coin_spends)],
        )
        assert absorbed1 == set()

        p2, data2 = make_payment(coin1, lineage1, launched.derivation, amounts[1])
        signed2 = sign_payment(p2, data2)
        coin2, lineage2, absorbed2 = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", p1.coin_spends), ("p2", p2.coin_spends)],
        )
        assert absorbed2 == set()

        p3, data3 = make_payment(coin2, lineage2, launched.derivation, amounts[2])
        signed3 = sign_payment(p3, data3)
        coin3, _, absorbed3 = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", p1.coin_spends), ("p2", p2.coin_spends), ("p3", p3.coin_spends)],
        )
        assert absorbed3 == set()

        assert await push_bundle(launched.sim_client, signed2) == (MempoolInclusionStatus.FAILED, Err.UNKNOWN_UNSPENT)
        assert await push_bundle(launched.sim_client, signed1) == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock - 100)
        assert await push_bundle(launched.sim_client, signed2) == (
            MempoolInclusionStatus.FAILED,
            Err.ASSERT_SECONDS_RELATIVE_FAILED,
        )
        await pass_time_and_farm(launched.sim, 100)
        assert await push_bundle(launched.sim_client, signed2) == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock - 100)
        assert await push_bundle(launched.sim_client, signed3) == (
            MempoolInclusionStatus.FAILED,
            Err.ASSERT_SECONDS_RELATIVE_FAILED,
        )
        await pass_time_and_farm(launched.sim, 100)
        assert await push_bundle(launched.sim_client, signed3) == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        record = await launched.sim_client.get_coin_record_by_name(coin3.name())
        assert record is not None
        assert record.coin == coin3
        assert record.coin.amount == uint64(int(launched.singleton.amount) - sum(amounts))


@pytest.mark.asyncio
@PROFILES
async def test_series_absorbs_disjoint_p2_singletons(profile: TimelockProfile) -> None:
    """Consecutive payments absorb disjoint p2 singleton coins."""
    async with launch_with_profile(profile) as launched:
        p2_puzzle_hash = construct_p2_singleton(launched.derivation.prefarm_info.launcher_id).get_tree_hash()
        launch_records = await launched.sim_client.get_coin_records_by_puzzle_hashes(
            [p2_puzzle_hash], include_spent_coins=False
        )
        assert len(launch_records) == 1
        launch_p2 = launch_records[0].coin
        await create_p2_singleton_coins(
            launched.sim,
            launched.sim_client,
            launched.derivation.prefarm_info.launcher_id,
            [5, 7, 9],
        )
        all_records = await launched.sim_client.get_coin_records_by_puzzle_hashes(
            [p2_puzzle_hash], include_spent_coins=False
        )
        all_p2_coins = [record.coin for record in all_records]

        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)
        p1_amount = int(launch_p2.amount)
        p2_amount = 20

        p1, data1 = make_payment(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            p1_amount,
            p2_singletons=[launch_p2],
        )
        signed1 = sign_payment(p1, data1)
        coin1, lineage1, absorbed1 = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", p1.coin_spends)],
        )
        assert absorbed1 == {launch_p2.name()}
        assert int(launch_p2.amount) % 2 == 0

        remaining = [coin for coin in all_p2_coins if int(coin.amount) in (7, 9)]
        assert {int(coin.amount) for coin in remaining} == {7, 9}
        assert sum(int(coin.amount) for coin in remaining) % 2 == 0

        p2, data2 = make_payment(coin1, lineage1, launched.derivation, p2_amount, p2_singletons=remaining)
        signed2 = sign_payment(p2, data2)
        _, _, absorbed2 = advance_through_previous_payments(
            coin1,
            lineage1,
            launched.derivation,
            [("p2", p2.coin_spends)],
        )
        final_coin, _, all_absorbed = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", p1.coin_spends), ("p2", p2.coin_spends)],
        )
        assert absorbed1.isdisjoint(absorbed2)
        assert absorbed2 == {coin.name() for coin in remaining}
        assert all_absorbed == absorbed1 | absorbed2
        assert sum(int(coin.amount) for coin in remaining) % 2 == 0

        assert await push_bundle(launched.sim_client, signed1) == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()
        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)
        assert await push_bundle(launched.sim_client, signed2) == (MempoolInclusionStatus.SUCCESS, None)
        await launched.sim.farm_block()

        record = await launched.sim_client.get_coin_record_by_name(final_coin.name())
        assert record is not None
        assert record.coin == final_coin
        absorbed_sum = int(launch_p2.amount) + sum(int(coin.amount) for coin in remaining)
        assert record.coin.amount == uint64(int(launched.singleton.amount) - p1_amount - p2_amount + absorbed_sum)


@pytest.mark.asyncio
async def test_wrong_order_and_stale_file_rejected() -> None:
    """A file pushed before its predecessor and a file for an already spent singleton are rejected."""
    profile = SHORT_TIMELOCKS
    async with launch_with_profile(profile) as launched:
        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)
        p1, data1 = make_payment(launched.singleton, launched.lineage, launched.derivation, 10)
        coin1, lineage1, _ = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", p1.coin_spends)],
        )
        p2, _ = make_payment(coin1, lineage1, launched.derivation, 20)

        wrong_order = "p2 does not spend the expected singleton coin " + launched.singleton.name().hex()
        with pytest.raises(ValueError, match=re.escape(wrong_order)):
            advance_through_previous_payments(
                launched.singleton,
                launched.lineage,
                launched.derivation,
                [("p2", p2.coin_spends), ("p1", p1.coin_spends)],
            )

        assert await push_bundle(launched.sim_client, sign_payment(p1, data1)) == (
            MempoolInclusionStatus.SUCCESS,
            None,
        )
        await launched.sim.farm_block()

        already_spent = "p1 does not spend the expected singleton coin " + coin1.name().hex()
        with pytest.raises(ValueError, match=re.escape(already_spent)):
            advance_through_previous_payments(
                coin1,
                lineage1,
                launched.derivation,
                [("p1", p1.coin_spends)],
            )


@pytest.mark.asyncio
async def test_non_payment_file_rejected(tmp_path: Path) -> None:
    """Files that are not payment spends cannot be chained."""
    profile = SHORT_TIMELOCKS
    async with launch_with_profile(profile) as launched:
        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)
        payment, data = make_payment(launched.singleton, launched.lineage, launched.derivation, 10)
        assert await push_bundle(launched.sim_client, sign_payment(payment, data)) == (
            MempoolInclusionStatus.SUCCESS,
            None,
        )
        await launched.sim.farm_block()
        new_coin, new_lineage, _ = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", payment.coin_spends)],
        )

        new_derivation = calculate_puzzle_root(
            launched.derivation.prefarm_info,
            _pubkeys(range(5, 10)),
            uint32(3),
            uint32(5),
            uint32(1),
        )
        rekey_bundle, _ = get_rekey_spend_info(
            new_coin,
            _pubkeys(range(3)),
            launched.derivation,
            new_lineage,
            new_derivation,
        )
        rekey_path = str(tmp_path / "start_rekey.spend")
        bundle_to_unsigned_spend_file(rekey_path, rekey_bundle)
        rekey_spends = load_previous_spend(rekey_path)
        with pytest.raises(
            ValueError,
            match=re.escape("start_rekey.spend: only payment spends can be chained"),
        ):
            advance_through_previous_payments(
                new_coin,
                new_lineage,
                launched.derivation,
                [("start_rekey.spend", rekey_spends)],
            )

        ach_coin = Coin(
            launched.singleton.name(),
            curry_ach_puzzle(launched.derivation.prefarm_info, ACS_PH).get_tree_hash(),
            uint64(10),
        )
        ach_bundle, _ = get_ach_clawback_spend_info(
            ach_coin,
            _pubkeys(range(3)),
            launched.derivation,
            ACS_PH,
        )
        ach_path = str(tmp_path / "ach_clawback.spend")
        bundle_to_unsigned_spend_file(ach_path, ach_bundle)
        ach_spends = load_previous_spend(ach_path)
        no_singleton = (
            "ach_clawback.spend: expected exactly one singleton spend; "
            "only payment spends generated by cic payment can be chained"
        )
        with pytest.raises(ValueError, match=re.escape(no_singleton)):
            advance_through_previous_payments(
                new_coin,
                new_lineage,
                launched.derivation,
                [("ach_clawback.spend", ach_spends)],
            )

        rekey_coin = next(coin for coin in rekey_bundle.additions() if coin.amount == 0)
        rekey_clawback_bundle, _ = get_rekey_clawback_spend_info(
            rekey_coin,
            _pubkeys(range(3)),
            launched.derivation,
            uint8(1),
            new_derivation,
        )
        rekey_clawback_path = str(tmp_path / "rekey_clawback.spend")
        bundle_to_unsigned_spend_file(rekey_clawback_path, rekey_clawback_bundle)
        rekey_clawback_spends = load_previous_spend(rekey_clawback_path)
        rekey_no_singleton = (
            "rekey_clawback.spend: expected exactly one singleton spend; "
            "only payment spends generated by cic payment can be chained"
        )
        with pytest.raises(ValueError, match=re.escape(rekey_no_singleton)):
            advance_through_previous_payments(
                new_coin,
                new_lineage,
                launched.derivation,
                [("rekey_clawback.spend", rekey_clawback_spends)],
            )


@pytest.mark.asyncio
async def test_spend_type_check_rejects_rekey(tmp_path: Path) -> None:
    """A rekey file is rejected by the spend type check, not by the amount reader."""
    profile = SHORT_TIMELOCKS
    async with launch_with_profile(profile) as launched:
        new_derivation = calculate_puzzle_root(
            launched.derivation.prefarm_info,
            _pubkeys(range(5, 10)),
            uint32(3),
            uint32(5),
            uint32(1),
        )
        rekey_bundle, _ = get_rekey_spend_info(
            launched.singleton,
            _pubkeys(range(3)),
            launched.derivation,
            launched.lineage,
            new_derivation,
        )
        path = str(tmp_path / "start_rekey.spend")
        bundle_to_unsigned_spend_file(path, rekey_bundle)
        rekey_spends = load_previous_spend(path)

        with pytest.raises(
            ValueError,
            match=re.escape("start_rekey.spend: only payment spends can be chained"),
        ):
            advance_through_previous_payments(
                launched.singleton,
                launched.lineage,
                launched.derivation,
                [("start_rekey.spend", rekey_spends)],
            )


@pytest.mark.asyncio
@PROFILES
async def test_series_cannot_be_one_bundle(profile: TimelockProfile) -> None:
    """Two payments from one series cannot share a bundle."""
    async with launch_with_profile(profile) as launched:
        p1, data1 = make_payment(launched.singleton, launched.lineage, launched.derivation, 10)
        coin1, lineage1, _ = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", p1.coin_spends)],
        )
        p2, data2 = make_payment(coin1, lineage1, launched.derivation, 20)
        combined = SpendBundle.aggregate([sign_payment(p1, data1), sign_payment(p2, data2)])

        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)
        assert await push_bundle(launched.sim_client, combined) == (
            MempoolInclusionStatus.FAILED,
            Err.EPHEMERAL_RELATIVE_CONDITION,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["even", "zero", "negative"])
async def test_amount_checks(tmp_path: Path, case: str) -> None:
    """Invalid resulting singleton amounts are rejected with the amount in the message."""
    profile = SHORT_TIMELOCKS
    async with launch_with_profile(profile) as launched:
        payment, _ = make_payment(launched.singleton, launched.lineage, launched.derivation, 10)
        current_amount = int(launched.singleton.amount)
        if case == "even":
            out_amount = 1
        elif case == "zero":
            out_amount = current_amount
        else:
            out_amount = current_amount + 2
        expected_amount = current_amount - out_amount

        singleton_spend = payment.coin_spends[0]
        solution = Program.from_bytes(bytes(singleton_spend.solution)).as_python()
        solution[2][2][0] = out_amount
        modified_spend = singleton_spend.replace(solution=Program.to(solution).to_serialized())
        modified_bundle = SpendBundle([modified_spend], G2Element())

        path = str(tmp_path / f"p1_{case}.spend")
        bundle_to_unsigned_spend_file(path, modified_bundle)
        coin_spends = load_previous_spend(path)

        message = (
            f"p1: the resulting singleton amount {expected_amount} must be positive and odd; "
            "check the payment amount and the absorbed coins, then regenerate this file"
        )
        with pytest.raises(ValueError, match=re.escape(message)):
            advance_through_previous_payments(
                launched.singleton,
                launched.lineage,
                launched.derivation,
                [("p1", coin_spends)],
            )


@pytest.mark.asyncio
async def test_series_invalidated_by_intervening_rekey() -> None:
    """Any other spend of the singleton between two payment files leaves the later file spending a coin that no
    longer exists."""
    profile = LONG_TIMELOCKS
    async with launch_with_profile(profile) as launched:
        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)
        p1, data1 = make_payment(launched.singleton, launched.lineage, launched.derivation, 10)
        assert await push_bundle(launched.sim_client, sign_payment(p1, data1)) == (
            MempoolInclusionStatus.SUCCESS,
            None,
        )
        await launched.sim.farm_block()
        coin1, lineage1, _ = advance_through_previous_payments(
            launched.singleton,
            launched.lineage,
            launched.derivation,
            [("p1", p1.coin_spends)],
        )
        p2, data2 = make_payment(coin1, lineage1, launched.derivation, 20)
        signed2 = sign_payment(p2, data2)

        await pass_time_and_farm(launched.sim, profile.rekey_increments)
        new_derivation = calculate_puzzle_root(
            launched.derivation.prefarm_info,
            _pubkeys(range(5, 10)),
            uint32(3),
            uint32(5),
            uint32(1),
        )
        start_rekey, rekey_data = get_rekey_spend_info(
            coin1,
            _pubkeys(range(3)),
            launched.derivation,
            lineage1,
            new_derivation,
        )
        assert await push_bundle(launched.sim_client, sign_payment(start_rekey, rekey_data)) == (
            MempoolInclusionStatus.SUCCESS,
            None,
        )
        await launched.sim.farm_block()

        await pass_time_and_farm(launched.sim, profile.withdrawal_timelock)
        assert await push_bundle(launched.sim_client, signed2) == (MempoolInclusionStatus.FAILED, Err.DOUBLE_SPEND)
