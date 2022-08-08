import dataclasses
import itertools
import pytest

from blspy import AugSchemeMPL, BasicSchemeMPL, G1Element, G2Element, PrivateKey
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
from chia.util.ints import uint8, uint32, uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.p2_conditions import puzzle_for_conditions
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    calculate_synthetic_offset,
    DEFAULT_HIDDEN_PUZZLE_HASH,
    puzzle_for_pk,
    solution_for_delegated_puzzle,
)
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH

from cic.drivers.drop_coins import curry_ach_puzzle, solve_ach_clawback, solve_rekey_clawback
from cic.drivers.filters import construct_rekey_filter, solve_filter_for_payment
from cic.drivers.merkle_utils import simplify_merkle_proof
from cic.drivers.prefarm import (
    construct_full_singleton,
    construct_singleton_inner_puzzle,
    get_withdrawal_spend_info,
    get_ach_clawforward_spend_bundle,
    get_ach_clawback_spend_info,
    get_rekey_spend_info,
    get_rekey_clawback_spend_info,
    get_rekey_completion_spend,
    calculate_rekey_args,
)
from cic.drivers.puzzle_root_construction import (
    calculate_puzzle_root,
    RootDerivation,
    get_all_aggregate_pubkey_combinations,
)
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.singleton import generate_launch_conditions_and_coin_spend, construct_p2_singleton

from tests.cost_logger import CostLogger

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


@pytest.fixture(scope="module")
def cost_logger():
    return CostLogger()


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
async def _setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)

    # Define constants
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
            bytes32([0] * 32),  # puzzle_root: bytes32  # gets set in calculate_puzzle_root
            WITHDRAWAL_TIMELOCK,  # withdrawal_timelock: uint64
            PAYMENT_CLAWBACK_PERIOD,  # payment_clawback_period: uint64
            REKEY_CLAWBACK_PERIOD,  # rekey_clawback_period: uint64
            REKEY_INCREMENTS,  # rekey_increments: uint64
            SLOW_REKEY_TIMELOCK,  # slow_rekey_timelock: uint64
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
async def test_puzzle_root_derivation(_setup_info):
    setup_info = await _setup_info
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
            filter_proof, _ = derivation.get_proofs_of_inclusion(agg_pk, lock=False)
            hash, proof = list(filter_proof.items())[0]
            assert root == simplify_merkle_proof(hash, proof)

        for agg_pk in get_all_aggregate_pubkey_combinations(pubkeys, uint32(m + 1)):
            filter_proof, _ = derivation.get_proofs_of_inclusion(agg_pk, lock=True)
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
async def test_payments(_setup_info, cost_logger):
    setup_info = await _setup_info
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

        # First, we're going to try to withdraw using less pubkeys than necessary
        WITHDRAWAL_AMOUNT = uint64(10)
        too_few_bundle, _ = get_withdrawal_spend_info(
            setup_info.singleton,
            TWO_PUBKEYS,
            setup_info.derivation,
            setup_info.first_lineage_proof,
            WITHDRAWAL_AMOUNT,
            ACS_PH,
        )
        result = await setup_info.sim_client.push_tx(too_few_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(ValueError, match="clvm raise"):
            too_few_bundle.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, too_few_bundle.coin_spends[0].solution
            )

        # Next, we'll actually make sure a withdrawal works
        withdrawal_bundle, data_to_sign = get_withdrawal_spend_info(
            setup_info.singleton,
            THREE_PUBKEYS,
            setup_info.derivation,
            setup_info.first_lineage_proof,
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
        cost_logger.add_cost("Withdrawal", aggregated_bundle)

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
        assert await setup_info.sim_client.get_coin_record_by_name(new_singleton.name()) is not None
        assert await setup_info.sim_client.get_coin_record_by_name(ach_coin.name()) is not None

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
            uint64(1),
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
        with pytest.raises(ValueError, match="clvm raise"):
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
        cost_logger.add_cost("ACH Clawback Pre-Timelock", aggregated_bundle)

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
            LineageProof(
                setup_info.singleton.parent_coin_info,
                construct_singleton_inner_puzzle(setup_info.prefarm_info).get_tree_hash(),
                setup_info.singleton.amount,
            ),
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
        aggregated_bundle = SpendBundle.aggregate([aggregated_bundle_0, aggregated_bundle_1])
        result = await setup_info.sim_client.push_tx(aggregated_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("ACH Clawback/Claim/Re-Pay", aggregated_bundle)

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
        cost_logger.add_cost("ACH Clawforward", honest_clawforward)
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_rekeys(_setup_info, cost_logger):
    setup_info = await _setup_info
    try:
        # Pubkey setup
        ONE_PUBKEY: G1Element = secret_key_for_index(0).get_g1()
        TWO_PUBKEYS: List[G1Element] = [secret_key_for_index(i).get_g1() for i in range(0, 2)]
        THREE_PUBKEYS: List[G1Element] = [secret_key_for_index(i).get_g1() for i in range(0, 3)]
        FOUR_PUBKEYS: List[G1Element] = [secret_key_for_index(i).get_g1() for i in range(0, 4)]
        FIVE_PUBKEYS: List[G1Element] = [secret_key_for_index(i).get_g1() for i in range(0, 5)]
        NEW_PUBKEYS: List[G1Element] = [secret_key_for_index(i).get_g1() for i in range(5, 10)]
        THREE_NEW_PUBKEYS: List[G1Element] = [secret_key_for_index(i).get_g1() for i in range(5, 8)]
        AGG_TWO = G1Element()
        AGG_THREE = G1Element()
        AGG_THREE_NEW = G1Element()
        AGG_FOUR = G1Element()
        AGG_FIVE = G1Element()
        for pk in TWO_PUBKEYS:
            AGG_TWO += pk
        for pk in THREE_PUBKEYS:
            AGG_THREE += pk
        for pk in THREE_NEW_PUBKEYS:
            AGG_THREE_NEW += pk
        for pk in FOUR_PUBKEYS:
            AGG_FOUR += pk
        for pk in FIVE_PUBKEYS:
            AGG_FIVE += pk

        # Let's try increasing the lock level all the way to the max
        # Bump from 3 -> 4
        three_to_four_bundle, data_to_sign = get_rekey_spend_info(
            setup_info.singleton,
            FOUR_PUBKEYS,
            setup_info.derivation,
            setup_info.first_lineage_proof,
        )
        signature: G2Element = AugSchemeMPL.aggregate(
            [sign_message_at_index(i, data_to_sign, AGG_FOUR) for i in range(0, 4)]
        )
        aggregate_bundle = SpendBundle.aggregate([three_to_four_bundle, SpendBundle([], signature)])
        # Before we actually perform a successful one, let's try to cancel it ephemerally and make sure that fails
        lock_completion_spend: CoinSpend = [
            cs
            for cs in aggregate_bundle.coin_spends
            if cs.coin.parent_coin_info == setup_info.singleton.name() and cs.coin.amount == 0
        ][0]
        singleton_spend: CoinSpend = [cs for cs in aggregate_bundle.coin_spends if cs.coin == setup_info.singleton][0]
        _, new_puzzle_root, filter_puzzle, filter_proof, filter_solution, extra_data_to_sign = calculate_rekey_args(
            lock_completion_spend.coin,
            FOUR_PUBKEYS,
            setup_info.derivation,
        )
        cancellation_spend: CoinSpend = dataclasses.replace(
            lock_completion_spend,
            solution=solve_rekey_clawback(
                setup_info.prefarm_info,
                new_puzzle_root,
                filter_puzzle,
                filter_proof,
                filter_solution,
            ),
        )
        extra_signature: G2Element = AugSchemeMPL.aggregate(
            [sign_message_at_index(i, data_to_sign, AGG_FOUR) for i in range(0, 4)],
        )
        ephemeral_cancel_bundle = SpendBundle.aggregate(
            [SpendBundle([singleton_spend, cancellation_spend], signature), SpendBundle([], extra_signature)]
        )
        result = await setup_info.sim_client.push_tx(ephemeral_cancel_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(ValueError, match="clvm raise"):
            cancellation_spend.puzzle_reveal.run_with_cost(INFINITE_COST, cancellation_spend.solution)
        # Now actually push the honest bundle
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Lock Level 3 -> 4", aggregate_bundle)

        # Then make sure there's no way to bump it with 3 again
        new_derivation: RootDerivation = calculate_puzzle_root(
            setup_info.prefarm_info,
            setup_info.derivation.pubkey_list,
            uint32(4),  # lock level 4
            uint32(5),
            uint32(1),
        )
        ephemeral_singleton = Coin(
            setup_info.singleton.name(),
            setup_info.singleton.puzzle_hash,
            setup_info.singleton.amount,
        )
        new_singleton = Coin(
            ephemeral_singleton.name(),
            construct_full_singleton(new_derivation.prefarm_info).get_tree_hash(),
            setup_info.singleton.amount,
        )
        new_lineage_proof = LineageProof(
            ephemeral_singleton.parent_coin_info,
            construct_singleton_inner_puzzle(setup_info.prefarm_info).get_tree_hash(),
            ephemeral_singleton.amount,
        )
        with pytest.raises(ValueError, match="Could not find a puzzle matching the specified pubkey"):
            _, _ = get_rekey_spend_info(
                new_singleton,
                FOUR_PUBKEYS,
                new_derivation,
                new_lineage_proof,
            )

        # Then 4 -> 5
        four_to_five_bundle, data_to_sign = get_rekey_spend_info(
            new_singleton,
            FIVE_PUBKEYS,
            new_derivation,
            new_lineage_proof,
        )
        signature: G2Element = AugSchemeMPL.aggregate(
            [sign_message_at_index(i, data_to_sign, AGG_FIVE) for i in range(0, 5)]
        )
        aggregate_bundle = SpendBundle.aggregate([four_to_five_bundle, SpendBundle([], signature)])
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Lock Level 4 -> 5", aggregate_bundle)

        # Then make sure we can't increase any further
        previous_prefarm_info: PrefarmInfo = new_derivation.prefarm_info
        new_derivation = calculate_puzzle_root(
            setup_info.prefarm_info,
            setup_info.derivation.pubkey_list,
            uint32(5),  # lock level 5
            uint32(5),
            uint32(1),
        )
        ephemeral_singleton = Coin(
            new_singleton.name(),
            new_singleton.puzzle_hash,
            new_singleton.amount,
        )
        new_singleton = Coin(
            ephemeral_singleton.name(),
            construct_full_singleton(new_derivation.prefarm_info).get_tree_hash(),
            setup_info.singleton.amount,
        )
        new_lineage_proof = LineageProof(
            ephemeral_singleton.parent_coin_info,
            construct_singleton_inner_puzzle(previous_prefarm_info).get_tree_hash(),
            ephemeral_singleton.amount,
        )
        with pytest.raises(AssertionError):
            _, _ = get_rekey_spend_info(
                new_singleton,
                FOUR_PUBKEYS,
                new_derivation,
                new_lineage_proof,
            )

        # Now let's rekey down to a new 2/3 pubkey set
        # Start with a slower rekey of 3 keys
        re_derivation: RootDerivation = calculate_puzzle_root(
            new_derivation.prefarm_info,
            NEW_PUBKEYS,
            uint64(3),
            uint64(5),
            uint64(1),
        )
        start_slow_rekey_bundle, data_to_sign = get_rekey_spend_info(
            new_singleton,
            THREE_PUBKEYS,
            new_derivation,
            new_lineage_proof,
            re_derivation,
        )
        synth_pk: G1Element = get_synthetic_pubkey(AGG_THREE)
        signature: G2Element = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(AGG_THREE, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(0, 3)],
            ]
        )
        aggregate_bundle = SpendBundle.aggregate([start_slow_rekey_bundle, SpendBundle([], signature)])
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)
        setup_info.sim.pass_time(
            setup_info.prefarm_info.slow_rekey_timelock + setup_info.prefarm_info.rekey_increments * 3
        )
        await setup_info.sim.farm_block()
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Start Slow Rekey", aggregate_bundle)

        # Try to cancel with 1 key
        rekey_coin: Coin = [c for c in start_slow_rekey_bundle.additions() if c.amount == 0][0]
        rekey_lineage = LineageProof(
            new_singleton.parent_coin_info,
            construct_singleton_inner_puzzle(new_derivation.prefarm_info).get_tree_hash(),
            new_singleton.amount,
        )
        too_few_cancel_bundle, data_to_sign = get_rekey_clawback_spend_info(
            rekey_coin,
            [ONE_PUBKEY],
            new_derivation,
            uint8(3),
            re_derivation,
        )
        synth_pk: G1Element = get_synthetic_pubkey(ONE_PUBKEY)
        signature: G2Element = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(ONE_PUBKEY, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(0, 3)],
            ]
        )
        aggregate_bundle = SpendBundle.aggregate([too_few_cancel_bundle, SpendBundle([], signature)])
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(ValueError, match="clvm raise"):
            aggregate_bundle.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, aggregate_bundle.coin_spends[0].solution
            )

        # Cancel with the 3 keys
        REWIND_HERE: uint32 = setup_info.sim.block_height
        same_number_cancel_bundle, data_to_sign = get_rekey_clawback_spend_info(
            rekey_coin,
            THREE_PUBKEYS,
            new_derivation,
            uint8(3),
            re_derivation,
        )
        synth_pk: G1Element = get_synthetic_pubkey(AGG_THREE)
        signature: G2Element = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(AGG_THREE, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(0, 3)],
            ]
        )
        aggregate_bundle = SpendBundle.aggregate([same_number_cancel_bundle, SpendBundle([], signature)])
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Cancel Slow Rekey", aggregate_bundle)
        # Check that no coins were created
        assert len(aggregate_bundle.additions()) == 0

        # Make sure completion works
        await setup_info.sim.rewind(REWIND_HERE)
        new_lineage_proof = LineageProof(
            new_singleton.parent_coin_info,
            construct_singleton_inner_puzzle(new_derivation.prefarm_info).get_tree_hash(),
            new_singleton.amount,
        )
        new_singleton = Coin(
            new_singleton.name(),
            new_singleton.puzzle_hash,
            new_singleton.amount,
        )
        finish_rekey_spend: SpendBundle = get_rekey_completion_spend(
            new_singleton,
            rekey_coin,
            THREE_PUBKEYS,
            new_derivation,
            new_lineage_proof,
            rekey_lineage,
            re_derivation,
        )
        # Make sure it's only possible after the appropriate amount of time
        result = await setup_info.sim_client.push_tx(finish_rekey_spend)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)
        setup_info.sim.pass_time(setup_info.prefarm_info.rekey_clawback_period)
        await setup_info.sim.farm_block()
        result = await setup_info.sim_client.push_tx(finish_rekey_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Finish Slow Rekey", aggregate_bundle)

        # Initiate a rekey with the new key set
        new_derivation = re_derivation
        new_lineage_proof = LineageProof(
            new_singleton.parent_coin_info, new_lineage_proof.inner_puzzle_hash, new_singleton.amount
        )
        new_singleton = Coin(
            new_singleton.name(),
            construct_full_singleton(new_derivation.prefarm_info).get_tree_hash(),
            new_singleton.amount,
        )
        start_fast_rekey_bundle, data_to_sign = get_rekey_spend_info(
            new_singleton,
            THREE_NEW_PUBKEYS,
            new_derivation,
            new_lineage_proof,
            setup_info.derivation,  # Back to the original set of keys
        )
        synth_pk: G1Element = get_synthetic_pubkey(AGG_THREE_NEW)
        signature: G2Element = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(AGG_THREE_NEW, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(5, 8)],
            ]
        )
        aggregate_bundle = SpendBundle.aggregate([start_fast_rekey_bundle, SpendBundle([], signature)])
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)
        setup_info.sim.pass_time(setup_info.prefarm_info.rekey_increments)
        await setup_info.sim.farm_block()
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Start Rekey", aggregate_bundle)

        # Try to maliciously rekey the singleton with an announcement
        rekey_coin: Coin = [c for c in start_slow_rekey_bundle.additions() if c.amount == 0][0]
        rekey_lineage = LineageProof(
            new_singleton.parent_coin_info,
            construct_singleton_inner_puzzle(new_derivation.prefarm_info).get_tree_hash(),
            new_singleton.amount,
        )
        new_lineage_proof = LineageProof(
            new_singleton.parent_coin_info, new_lineage_proof.inner_puzzle_hash, new_singleton.amount
        )
        new_singleton = Coin(
            new_singleton.name(),
            construct_full_singleton(new_derivation.prefarm_info).get_tree_hash(),
            new_singleton.amount,
        )

        accidental_rekey_spend: SpendBundle = get_rekey_completion_spend(
            new_singleton,
            rekey_coin,
            THREE_NEW_PUBKEYS,
            new_derivation,
            new_lineage_proof,
            rekey_lineage,
            setup_info.derivation,
        )

        malicious_rekey_clawback_bundle, data_to_sign = get_rekey_clawback_spend_info(
            rekey_coin,
            THREE_NEW_PUBKEYS,
            new_derivation,
            setup_info.prefarm_info.rekey_increments,
            setup_info.derivation,
            additional_conditions=[Program.to([62, "r"])],
        )
        synth_pk: G1Element = get_synthetic_pubkey(AGG_THREE_NEW)
        signature: G2Element = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(AGG_THREE_NEW, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(5, 8)],
            ]
        )
        aggregate_bundle = SpendBundle.aggregate(
            [accidental_rekey_spend, malicious_rekey_clawback_bundle, SpendBundle([], signature)]
        )
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(ValueError, match="clvm raise"):
            malicious_rekey_clawback_bundle.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, malicious_rekey_clawback_bundle.coin_spends[0].solution
            )

        # Try to include an announcement in a withdrawal to maliciously cancel the rekey
        malicious_withdrawal_bundle, data_to_sign = get_withdrawal_spend_info(
            new_singleton,
            THREE_NEW_PUBKEYS,
            new_derivation,
            new_lineage_proof,
            uint64(10),
            ACS_PH,
            additional_conditions=[Program.to([62, "r"])],
        )
        synth_pk: G1Element = get_synthetic_pubkey(AGG_THREE_NEW)
        signature: G2Element = AugSchemeMPL.aggregate(
            [
                sign_message_with_offset(AGG_THREE_NEW, data_to_sign, synth_pk),
                *[sign_message_at_index(i, data_to_sign, synth_pk) for i in range(5, 8)],
            ]
        )
        aggregate_bundle = SpendBundle.aggregate(
            [accidental_rekey_spend, malicious_withdrawal_bundle, SpendBundle([], signature)]
        )
        result = await setup_info.sim_client.push_tx(aggregate_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(ValueError, match="clvm raise"):
            malicious_withdrawal_bundle.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, malicious_withdrawal_bundle.coin_spends[0].solution
            )
    finally:
        await setup_info.sim.close()


def test_cost(cost_logger):
    cost_logger.log_cost_statistics()
