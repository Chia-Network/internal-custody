import itertools
import pytest

from blspy import BasicSchemeMPL, G1Element, G2Element, PrivateKey
from dataclasses import dataclass
from typing import List

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.util.hash import std_hash
from chia.util.ints import uint32, uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH

from cic.drivers.merkle_utils import simplify_merkle_proof
from cic.drivers.prefarm import construct_singleton_inner_puzzle
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


def secret_key_for_index(index: int) -> PrivateKey:
    blob = index.to_bytes(32, "big")
    hashed_blob = BasicSchemeMPL.key_gen(std_hash(b"foo" + blob))
    secret_exponent = int.from_bytes(hashed_blob, "big")
    return PrivateKey.from_bytes(secret_exponent.to_bytes(32, "big"))


@pytest.fixture(scope="function")
async def setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)

    # Define constants
    START_DATE = uint64(sim.timestamp)
    DRAIN_RATE = 1  # 1 mojo per second
    PUBKEY_LIST = [secret_key_for_index(i).get_g1() for i in range(0, 5)]
    WITHDRAWAL_TIMELOCK = uint64(2592000)  # 30 days
    PAYMENT_CLAWBACK_PERIOD = uint64(90)
    REKEY_CLAWBACK_PERIOD = uint64(60)
    SLOW_REKEY_TIMELOCK = uint64(0)
    REKEY_INCREMENTS = uint64(0)

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
            for lock in (True, False):
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
