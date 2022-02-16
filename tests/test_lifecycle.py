import pytest

from blspy import G2Element
from dataclasses import dataclass

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer import pay_to_singleton_puzzle

from cic.drivers.prefarm import construct_singleton_inner_puzzle
from cic.drivers.singleton import generate_launch_conditions_and_coin_spend

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
    initial_drain_date: uint64
    drain_rate: uint64


@pytest.fixture(scope="function")
async def setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)

    # Define constants
    INITIAL_DRAIN_DATE = uint64(sim.timestamp)
    DRAIN_RATE = 1  # 1 mojo per second

    # Identify the prefarm coins
    prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])
    big_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 18375000000000000000)
    small_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 2625000000000000000)

    # Launch them to their starting state
    conditions, launch_spend = generate_launch_conditions_and_coin_spend(
        big_coin, construct_singleton_inner_puzzle(INITIAL_DRAIN_DATE, DRAIN_RATE, ACS), 18374999999999999999
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
                    [[51, pay_to_singleton_puzzle(launch_spend.coin.name()).get_tree_hash(), 2625000000000000000]]
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
        INITIAL_DRAIN_DATE,
        DRAIN_RATE,
    )


@pytest.mark.asyncio
async def test_setup(setup_info):
    try:
        pass
    finally:
        await setup_info.sim.close()
