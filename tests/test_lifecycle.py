import itertools
import pytest

from blspy import AugSchemeMPL, BasicSchemeMPL, G1Element, G2Element, PrivateKey
from clvm.EvalError import EvalError
from dataclasses import dataclass
from typing import List, Optional

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program, INFINITE_COST
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.util.errors import Err
from chia.util.hash import std_hash
from chia.util.ints import uint32, uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.p2_conditions import puzzle_for_conditions
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    calculate_synthetic_offset,
    DEFAULT_HIDDEN_PUZZLE_HASH,
    puzzle_for_pk,
    solution_for_delegated_puzzle,
)
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH

from cic.drivers.drop_coins import curry_ach_puzzle, solve_ach_clawback
from cic.drivers.filters import construct_rekey_filter, solve_filter_for_payment
from cic.drivers.merkle_utils import simplify_merkle_proof
from cic.drivers.prefarm import (
    construct_singleton_inner_puzzle,
    get_withdrawal_spend_info,
    get_ach_clawforward_spend_bundle,
    get_ach_clawback_spend_info,
)
from cic.drivers.puzzle_root_construction import (
    calculate_puzzle_root,
    RootDerivation,
    get_all_aggregate_pubkey_combinations,
)
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.singleton import generate_launch_conditions_and_coin_spend, construct_p2_singleton

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


@dataclass
class SetupInfo:
    sim: SpendSim
    sim_client: SimClient
    singleton: Coin
    p2_singleton: Coin
    launcher_id: bytes32
    first_lineage_proof: LineageProof
    prefarm_info: PrefarmInfo
    derivation: RootDerivation


# Key functions
def secret_key_for_index(index: int) -> PrivateKey:
    blob = index.to_bytes(32, "big")
    hashed_blob = BasicSchemeMPL.key_gen(std_hash(b"foo" + blob))
    secret_exponent = int.from_bytes(hashed_blob, "big")
    return PrivateKey.from_bytes(secret_exponent.to_bytes(32, "big"))


def get_synthetic_pubkey(pk: G1Element) -> G1Element:
    secret_exponent = calculate_synthetic_offset(pk, DEFAULT_HIDDEN_PUZZLE_HASH)
    sk = PrivateKey.from_bytes(secret_exponent.to_bytes(32, "big"))
    return sk.get_g1() + pk


def sign_message_at_index(index: int, msg: bytes, agg_pk: Optional[G1Element] = None) -> G2Element:
    sk: PrivateKey = secret_key_for_index(index)
    if agg_pk is not None:
        return AugSchemeMPL.sign(sk, msg, agg_pk)
    else:
        return AugSchemeMPL.sign(sk, msg)


def sign_message_with_offset(pk: G1Element, msg: bytes, agg_pk: Optional[G1Element] = None) -> G2Element:
    secret_exponent = calculate_synthetic_offset(pk, DEFAULT_HIDDEN_PUZZLE_HASH)
    sk = PrivateKey.from_bytes(secret_exponent.to_bytes(32, "big"))
    if agg_pk is not None:
        return AugSchemeMPL.sign(sk, msg, agg_pk)
    else:
        return AugSchemeMPL.sign(sk, msg)


@pytest.fixture(scope="function")
async def setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)

    # Define constants
    START_DATE = uint64(sim.timestamp)
    DRAIN_RATE = 77777777777  # mojos per second
    PUBKEY_LIST = [secret_key_for_index(i).get_g1() for i in range(0, 5)]
    WITHDRAWAL_TIMELOCK = uint64(2592000)  # 30 days
    PAYMENT_CLAWBACK_PERIOD = uint64(90)
    REKEY_CLAWBACK_PERIOD = uint64(60)
    SLOW_REKEY_TIMELOCK = uint64(45)
    REKEY_INCREMENTS = uint64(15)

    # Identify the prefarm coins
    prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])
    big_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 18375000000000000000)
    small_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 2625000000000000000)

    # Launch them to their starting state
    starting_amount = 18374999999999999999
    launcher_coin = Coin(big_coin.name(), SINGLETON_LAUNCHER_HASH, starting_amount)
    derivation: RootDerivation = calculate_puzzle_root(
        PrefarmInfo(
            launcher_coin.name(),  # launcher_id: bytes32
            START_DATE,  # start_date: uint64
            starting_amount,  # starting_amount: uint64
            DRAIN_RATE,  # mojos_per_second: uint64
            bytes32([0] * 32),  # puzzle_root: bytes32  # gets set in set_puzzle_root
            WITHDRAWAL_TIMELOCK,  # withdrawal_timelock: uint64
            PAYMENT_CLAWBACK_PERIOD,  # payment_clawback_period: uint64
            REKEY_CLAWBACK_PERIOD,  # rekey_clawback_period: uint64
            SLOW_REKEY_TIMELOCK,  # slow_rekey_timelock: uint64
            REKEY_INCREMENTS,  # rekey_increments: uint64
        ),
        PUBKEY_LIST,
        uint32(3),
        uint32(5),
        uint32(1),
    )
    conditions, launch_spend = generate_launch_conditions_and_coin_spend(
        big_coin, construct_singleton_inner_puzzle(derivation.prefarm_info), starting_amount
    )
    creation_bundle = SpendBundle(
        [
            CoinSpend(
                big_coin,
                ACS,
                Program.to(conditions),
            ),
            CoinSpend(
                small_coin,
                ACS,
                Program.to(
                    [[51, construct_p2_singleton(launch_spend.coin.name()).get_tree_hash(), 2625000000000000000]]
                ),
            ),
            launch_spend,
        ],
        G2Element(),
    )

    # Process the state
    await sim_client.push_tx(creation_bundle)
    await sim.farm_block()

    # Identify the coins again
    prefarm_coins = await sim.all_non_reward_coins()
    big_coin = next(coin for coin in prefarm_coins if coin.amount == 18374999999999999999)
    small_coin = next(coin for coin in prefarm_coins if coin.amount == 2625000000000000000)

    return SetupInfo(
        sim,
        sim_client,
        big_coin,
        small_coin,
        launch_spend.coin.name(),
        LineageProof(parent_name=launch_spend.coin.parent_coin_info, amount=launch_spend.coin.amount),
        derivation.prefarm_info,
        derivation,
    )


@pytest.mark.asyncio
async def test_puzzle_root_derivation(setup_info):
    try:
        pubkeys: List[G1Element] = []
        m = uint32(3)
        n = uint32(5)
        for i in range(0, 5):
            pubkeys.append(secret_key_for_index(i).get_g1())
        derivation: RootDerivation = calculate_puzzle_root(setup_info.prefarm_info, pubkeys, m, n, 1)
        root: bytes32 = derivation.prefarm_info.puzzle_root
        # Test a few iterations to make sure ordering doesn't matter
        for subset in list(itertools.permutations(pubkeys))[0:5]:
            assert root == calculate_puzzle_root(setup_info.prefarm_info, subset, m, n, 1).prefarm_info.puzzle_root

        for agg_pk in get_all_aggregate_pubkey_combinations(pubkeys, m):
            for lock in (True, True):
                filter_proof, _ = derivation.get_proofs_of_inclusion(agg_pk, lock=lock)
                hash, proof = list(filter_proof.items())[0]
                assert root == simplify_merkle_proof(hash, proof)

        for i in range(1, m):
            for agg_pk in get_all_aggregate_pubkey_combinations(pubkeys, 1):
                filter_proof, _ = derivation.get_proofs_of_inclusion(agg_pk)
                hash, proof = list(filter_proof.items())[0]
                assert root == simplify_merkle_proof(hash, proof)
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_setup(setup_info):
    try:
        pass
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_payments(setup_info):
    try:
        TWO_PUBKEYS: List[G1Element] = [secret_key_for_index(i).get_g1() for i in range(0, 2)]
        THREE_PUBKEYS: List[G1Element] = [secret_key_for_index(i).get_g1() for i in range(0, 3)]
        AGG_THREE = G1Element()
        AGG_TWO = G1Element()
        for pk in THREE_PUBKEYS:
            AGG_THREE += pk
        for pk in TWO_PUBKEYS:
            AGG_TWO += pk
        # Just pass a silly number of seconds to bypass the RL
        setup_info.sim.pass_time(uint64(1000000000000000))
        await setup_info.sim.farm_block()
        current_time = setup_info.sim.timestamp

        # First, we're going to try to withdraw using less pubkeys than necessary
        WITHDRAWAL_AMOUNT = uint64(10)
        too_few_bundle, _ = get_withdrawal_spend_info(
            setup_info.singleton,
            TWO_PUBKEYS,
            setup_info.derivation,
            setup_info.first_lineage_proof,
            current_time,
            WITHDRAWAL_AMOUNT,
            ACS_PH,
        )
        result = await setup_info.sim_client.push_tx(too_few_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(EvalError, match="clvm raise"):
            too_few_bundle.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, too_few_bundle.coin_spends[0].solution
            )

        # Next, we'll actually make sure a withdrawal works
        withdrawal_bundle, data_to_sign = get_withdrawal_spend_info(
            setup_info.singleton,
            THREE_PUBKEYS,
            setup_info.derivation,
            setup_info.first_lineage_proof,
            current_time,
            WITHDRAWAL_AMOUNT,
            ACS_PH,
        )
        synth_pk: G1Element = get_synthetic_pubkey(AGG_THREE)
        signature: G2Element = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(AGG_THREE, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(0, 3)],
            ]
        )
        aggregated_bundle = SpendBundle.aggregate([withdrawal_bundle, SpendBundle([], signature)])
        result = await setup_info.sim_client.push_tx(aggregated_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

        # Now let's make sure the clawback coin was created and that the singleton was recreated properly
        new_singleton = Coin(
            setup_info.singleton.name(),
            setup_info.singleton.puzzle_hash,
            setup_info.singleton.amount - WITHDRAWAL_AMOUNT,
        )
        ach_coin = Coin(
            setup_info.singleton.name(),
            curry_ach_puzzle(setup_info.prefarm_info, ACS_PH).get_tree_hash(),
            WITHDRAWAL_AMOUNT,
        )
        assert setup_info.sim_client.get_coin_record_by_name(new_singleton.name()) is not None
        assert setup_info.sim_client.get_coin_record_by_name(ach_coin.name()) is not None

        # Try to claw forward the ach immediately and make sure it fails
        too_fast_clawforward: SpendBundle = get_ach_clawforward_spend_bundle(
            ach_coin,
            setup_info.derivation,
            ACS_PH,
        )
        result = await setup_info.sim_client.push_tx(too_fast_clawforward)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)

        # Try to clawback the ach using a slower rekey filter (the filter should block it)
        filter_proof, leaf_proof = (
            list(proof.items())[0][1] for proof in setup_info.derivation.get_proofs_of_inclusion(AGG_TWO)
        )
        inner_puzzle: Program = puzzle_for_pk(AGG_TWO)
        filter_puzzle: Program = construct_rekey_filter(
            setup_info.prefarm_info,
            simplify_merkle_proof(inner_puzzle.get_tree_hash(), leaf_proof),
            setup_info.prefarm_info.rekey_increments,
        )
        delegated_puzzle: Program = puzzle_for_conditions(
            [[51, construct_p2_singleton(setup_info.prefarm_info.launcher_id), ach_coin.amount]]
        )
        inner_solution: Program = solution_for_delegated_puzzle(delegated_puzzle, Program.to([]))
        not_allowed_clawback = SpendBundle(
            [
                CoinSpend(
                    ach_coin,
                    curry_ach_puzzle(setup_info.prefarm_info, ACS_PH),
                    solve_ach_clawback(
                        setup_info.prefarm_info,
                        ach_coin.amount,
                        filter_puzzle,
                        filter_proof,
                        solve_filter_for_payment(
                            inner_puzzle,
                            leaf_proof,
                            inner_solution,
                            setup_info.prefarm_info.puzzle_root,
                            ACS_PH,
                        ),
                    ),
                ),
            ],
            G2Element(),
        )
        result = await setup_info.sim_client.push_tx(not_allowed_clawback)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(EvalError, match="clvm raise"):
            not_allowed_clawback.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, not_allowed_clawback.coin_spends[0].solution
            )

        # Now perform an honest clawback before the timelock
        REWIND_HERE: uint32 = setup_info.sim.block_height
        honest_ach_clawback_before, data_to_sign = get_ach_clawback_spend_info(
            ach_coin, THREE_PUBKEYS, setup_info.derivation, ACS_PH
        )
        synth_pk = get_synthetic_pubkey(AGG_THREE)
        signature = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(AGG_THREE, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(0, 3)],
            ]
        )
        aggregated_bundle = SpendBundle.aggregate([honest_ach_clawback_before, SpendBundle([], signature)])
        result = await setup_info.sim_client.push_tx(aggregated_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

        # Then perform an honest clawback after the timelock
        await setup_info.sim.rewind(REWIND_HERE)
        setup_info.sim.pass_time(
            max(setup_info.prefarm_info.payment_clawback_period, setup_info.prefarm_info.withdrawal_timelock)
        )
        await setup_info.sim.farm_block()
        honest_ach_clawback_after, data_to_sign = get_ach_clawback_spend_info(
            ach_coin, THREE_PUBKEYS, setup_info.derivation, ACS_PH
        )
        synth_pk = get_synthetic_pubkey(AGG_THREE)
        signature = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(AGG_THREE, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(0, 3)],
            ]
        )
        aggregated_bundle_0 = SpendBundle.aggregate([honest_ach_clawback_after, SpendBundle([], signature)])
        # defer checking success here to test ephemerality in the next test

        # After the clawback, let's see if we can immediately claim back the funds and make another payment
        p2_singleton_absorbtion, data_to_sign = get_withdrawal_spend_info(
            new_singleton,
            THREE_PUBKEYS,
            setup_info.derivation,
            LineageProof(setup_info.singleton.parent_coin_info, construct_singleton_inner_puzzle(setup_info.prefarm_info).get_tree_hash(), setup_info.singleton.amount),
            current_time,
            WITHDRAWAL_AMOUNT,
            ACS_PH,
            p2_singletons_to_claim=[
                Coin(
                    ach_coin.name(),
                    construct_p2_singleton(setup_info.prefarm_info.launcher_id).get_tree_hash(),
                    WITHDRAWAL_AMOUNT,
                )
            ],
        )
        synth_pk = get_synthetic_pubkey(AGG_THREE)
        signature = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(AGG_THREE, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(0, 3)],
            ]
        )
        aggregated_bundle_1 = SpendBundle.aggregate([p2_singleton_absorbtion, SpendBundle([], signature)])
        result = await setup_info.sim_client.push_tx(SpendBundle.aggregate([aggregated_bundle_0, aggregated_bundle_1]))
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

        # Finally, let's make sure an honest clawforward works
        await setup_info.sim.rewind(REWIND_HERE)
        setup_info.sim.pass_time(setup_info.prefarm_info.payment_clawback_period)
        honest_clawforward: SpendBundle = get_ach_clawforward_spend_bundle(
            ach_coin,
            setup_info.derivation,
            ACS_PH,
        )
        result = await setup_info.sim_client.push_tx(honest_clawforward)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
    finally:
        await setup_info.sim.close()
