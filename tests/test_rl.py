import math
import pytest

from blspy import G2Element
from dataclasses import dataclass

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.util.errors import Err
from chia.util.ints import uint64
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.rate_limiting import construct_rate_limiting_puzzle, solve_rate_limiting_puzzle
from cic.drivers.singleton import construct_singleton, generate_launch_conditions_and_coin_spend, solve_singleton

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
    launcher_id: bytes32
    first_lineage_proof: LineageProof
    start_date: uint64
    drain_rate: uint64


@pytest.fixture(scope="function")
async def setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)

    # Define constants
    START_DATE = uint64(sim.timestamp)
    DRAIN_RATE = 1  # 1 mojo per second

    # Identify the coin
    prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])
    coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 18375000000000000000)

    # Launch it to the starting state
    starting_amount = 18374999999999999999
    conditions, launch_spend = generate_launch_conditions_and_coin_spend(
        coin, construct_rate_limiting_puzzle(START_DATE, starting_amount, DRAIN_RATE, uint64(1), ACS), 18374999999999999999
    )
    creation_bundle = SpendBundle(
        [
            CoinSpend(
                coin,
                ACS,
                Program.to(conditions),
            ),
            launch_spend,
        ],
        G2Element(),
    )

    # Process the state
    await sim_client.push_tx(creation_bundle)
    await sim.farm_block()

    # Identify the coin again
    coin = next(coin for coin in (await sim.all_non_reward_coins()) if coin.amount == 18374999999999999999)

    return SetupInfo(
        sim,
        sim_client,
        coin,
        launch_spend.coin.name(),
        LineageProof(parent_name=launch_spend.coin.parent_coin_info, amount=launch_spend.coin.amount),
        START_DATE,
        DRAIN_RATE,
    )


@pytest.mark.asyncio
async def test_draining(setup_info, cost_logger):
    try:
        # Setup the conditions to drain
        TO_DRAIN = uint64(10)
        setup_info.sim.pass_time(uint64(math.ceil(TO_DRAIN / setup_info.drain_rate) + 1))
        drain_time: uint64 = setup_info.sim.timestamp
        await setup_info.sim.farm_block()

        # Construct the spend
        initial_rl_puzzle: Program = construct_rate_limiting_puzzle(
            setup_info.start_date, setup_info.singleton.amount, setup_info.drain_rate, uint64(1), ACS
        )
        first_drain_spend = SpendBundle(
            [
                CoinSpend(
                    setup_info.singleton,
                    construct_singleton(
                        setup_info.launcher_id,
                        initial_rl_puzzle,
                    ),
                    solve_singleton(
                        setup_info.first_lineage_proof,
                        setup_info.singleton.amount,
                        solve_rate_limiting_puzzle(
                            drain_time,
                            Program.to([[51, ACS_PH, setup_info.singleton.amount - TO_DRAIN]]),
                        ),
                    ),
                ),
            ],
            G2Element(),
        )

        # Process the results
        result = await setup_info.sim_client.push_tx(first_drain_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Drain some", first_drain_spend)

        # Find the new coin
        new_coin: Coin = (await setup_info.sim_client.get_coin_records_by_parent_ids([setup_info.singleton.name()]))[
            0
        ].coin

        # Setup again
        TO_DRAIN = uint64(20)
        setup_info.sim.pass_time(uint64(math.ceil(TO_DRAIN / setup_info.drain_rate) + 1))
        next_drain_time: uint64 = setup_info.sim.timestamp
        await setup_info.sim.farm_block()

        # Create the next spend
        second_drain_spend = SpendBundle(
            [
                CoinSpend(
                    new_coin,
                    construct_singleton(
                        setup_info.launcher_id,
                        initial_rl_puzzle,
                    ),
                    solve_singleton(
                        LineageProof(
                            setup_info.singleton.parent_coin_info,
                            initial_rl_puzzle.get_tree_hash(),
                            setup_info.singleton.amount,
                        ),
                        new_coin.amount,
                        solve_rate_limiting_puzzle(
                            next_drain_time,
                            Program.to([[51, ACS_PH, new_coin.amount - TO_DRAIN]]),
                        ),
                    ),
                ),
            ],
            G2Element(),
        )

        # Process the results
        result = await setup_info.sim_client.push_tx(second_drain_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Drain again", second_drain_spend)

        # Check the child's puzzle hash
        new_coin: Coin = (await setup_info.sim_client.get_coin_records_by_parent_ids([new_coin.name()]))[0].coin
        assert (
            new_coin.puzzle_hash
            == construct_singleton(
                setup_info.launcher_id,
                initial_rl_puzzle,
            ).get_tree_hash()
        )
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_cant_drain_more(setup_info, cost_logger):
    try:
        # Setup the conditions to drain
        TO_DRAIN = uint64(10)
        setup_info.sim.pass_time(uint64(0))  # Oops forgot to pass the time
        drain_time = uint64(math.ceil(TO_DRAIN / setup_info.drain_rate) + 1)
        await setup_info.sim.farm_block()

        # Construct the spend
        initial_rl_puzzle: Program = construct_rate_limiting_puzzle(
            setup_info.start_date, setup_info.singleton.amount, setup_info.drain_rate, uint64(1), ACS
        )
        drain_spend = SpendBundle(
            [
                CoinSpend(
                    setup_info.singleton,
                    construct_singleton(
                        setup_info.launcher_id,
                        initial_rl_puzzle,
                    ),
                    solve_singleton(
                        setup_info.first_lineage_proof,
                        setup_info.singleton.amount,
                        solve_rate_limiting_puzzle(
                            drain_time,
                            Program.to([[51, ACS_PH, setup_info.singleton.amount - TO_DRAIN]]),
                        ),
                    ),
                ),
            ],
            G2Element(),
        )

        # Make sure it fails
        result = await setup_info.sim_client.push_tx(drain_spend)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_ABSOLUTE_FAILED)
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_refill_is_ignored(setup_info, cost_logger):
    try:
        # First let's farm ourselves some funds
        await setup_info.sim.farm_block(ACS_PH)
        fund_coin: Coin = (
            await setup_info.sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
        )[0].coin

        # Construct a spend without any time having passed
        TO_REFILL = uint64(10)
        initial_rl_puzzle: Program = construct_rate_limiting_puzzle(
            setup_info.start_date, setup_info.singleton.amount, setup_info.drain_rate, uint64(1), ACS
        )
        refill_spend = SpendBundle(
            [
                CoinSpend(
                    setup_info.singleton,
                    construct_singleton(
                        setup_info.launcher_id,
                        initial_rl_puzzle,
                    ),
                    solve_singleton(
                        setup_info.first_lineage_proof,
                        setup_info.singleton.amount,
                        solve_rate_limiting_puzzle(
                            setup_info.start_date,
                            Program.to([[51, ACS_PH, setup_info.singleton.amount + TO_REFILL]]),
                        ),
                    ),
                ),
                CoinSpend(
                    fund_coin,
                    ACS,
                    Program.to([[51, ACS_PH, fund_coin.amount - TO_REFILL]]),
                ),
            ],
            G2Element(),
        )

        # Process the results
        result = await setup_info.sim_client.push_tx(refill_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Refill", refill_spend)

        # Check that the singleton is the same puzzle hash
        new_coin: Coin = (await setup_info.sim_client.get_coin_records_by_parent_ids([setup_info.singleton.name()]))[
            0
        ].coin
        assert new_coin.puzzle_hash == setup_info.singleton.puzzle_hash
    finally:
        await setup_info.sim.close()


def test_cost(cost_logger):
    cost_logger.log_cost_statistics()
