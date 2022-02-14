import pytest

from blspy import G2Element
from dataclasses import dataclass

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.wallet.puzzles.singleton_top_layer import pay_to_singleton_puzzle

from cic.drivers.prefarm import construct_inner_puzzle
from cic.drivers.singleton import generate_launch_conditions_and_coin_spend


@dataclass
class SetupInfo:
    sim: SpendSim
    sim_client: SimClient
    singleton: Coin
    p2_singleton: Coin
    launcher_id: bytes32


@pytest.fixture(scope="function")
async def setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(Program.to(1).get_tree_hash())

    # Identify the prefarm coins
    prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([Program.to(1).get_tree_hash()])
    big_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 18375000000000000000)
    small_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 2625000000000000000)

    # Launch them to their starting state
    conditions, launch_spend = generate_launch_conditions_and_coin_spend(
        big_coin, construct_inner_puzzle(), 18374999999999999999
    )
    creation_bundle = SpendBundle(
        [
            CoinSpend(
                big_coin,
                Program.to(1),
                Program.to(conditions),
            ),
            CoinSpend(
                small_coin,
                Program.to(1),
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
    )
