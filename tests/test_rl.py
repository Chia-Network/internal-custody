import math
import pytest

from blspy import G2Element
from clvm.EvalError import EvalError
from dataclasses import dataclass

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.rate_limiting import construct_rate_limiting_puzzle, solve_rate_limiting_puzzle
from cic.drivers.singleton import construct_singleton, generate_launch_conditions_and_coin_spend, solve_singleton

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


@dataclass
class SetupInfo:
    sim: SpendSim
    sim_client: SimClient
    singleton: Coin
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

    # Identify the coin
    prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])
    coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 18375000000000000000)

    # Launch it to the starting state
    conditions, launch_spend = generate_launch_conditions_and_coin_spend(
        coin, construct_rate_limiting_puzzle(INITIAL_DRAIN_DATE, DRAIN_RATE, ACS), 18374999999999999999
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
        INITIAL_DRAIN_DATE,
        DRAIN_RATE,
    )


@pytest.mark.asyncio
async def test_draining(setup_info):
    try:
        # Setup the conditions to drain
        TO_DRAIN = uint64(10)
        setup_info.sim.pass_time(uint64(math.ceil(TO_DRAIN / setup_info.drain_rate) + 1))
        drain_time: uint64 = setup_info.sim.timestamp
        await setup_info.sim.farm_block()

        # Construct the spend
        initial_rl_puzzle: Program = construct_rate_limiting_puzzle(
            setup_info.initial_drain_date, setup_info.drain_rate, ACS
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
                            setup_info.singleton.amount,
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
        new_rl_puzzle: Program = construct_rate_limiting_puzzle(drain_time, setup_info.drain_rate, ACS)
        second_drain_spend = SpendBundle(
            [
                CoinSpend(
                    new_coin,
                    construct_singleton(
                        setup_info.launcher_id,
                        new_rl_puzzle,
                    ),
                    solve_singleton(
                        LineageProof(
                            setup_info.singleton.parent_coin_info,
                            initial_rl_puzzle.get_tree_hash(),
                            setup_info.singleton.amount,
                        ),
                        new_coin.amount,
                        solve_rate_limiting_puzzle(
                            new_coin.amount,
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

        # Check the child's puzzle hash
        new_coin: Coin = (await setup_info.sim_client.get_coin_records_by_parent_ids([new_coin.name()]))[0].coin
        assert (
            new_coin.puzzle_hash
            == construct_singleton(
                setup_info.launcher_id,
                construct_rate_limiting_puzzle(next_drain_time, setup_info.drain_rate, ACS),
            ).get_tree_hash()
        )
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_cant_drain_more(setup_info):
    try:
        # Setup the conditions to drain
        TO_DRAIN = uint64(10)
        setup_info.sim.pass_time(uint64(0))  # Oops forgot to pass the time
        drain_time = uint64(math.ceil(TO_DRAIN / setup_info.drain_rate) + 1)
        await setup_info.sim.farm_block()

        # Construct the spend
        initial_rl_puzzle: Program = construct_rate_limiting_puzzle(
            setup_info.initial_drain_date, setup_info.drain_rate, ACS
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
                            setup_info.singleton.amount,
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
        assert result[0] == MempoolInclusionStatus.FAILED

        # Make sure the failure is due to a raise
        puzzle: Program = drain_spend.coin_spends[0].puzzle_reveal.to_program()
        solution: Program = drain_spend.coin_spends[0].solution.to_program()
        with pytest.raises(EvalError, match="clvm raise"):
            puzzle.run(solution)
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_refill_is_ignored(setup_info):
    try:
        # First let's farm ourselves some funds
        await setup_info.sim.farm_block(ACS_PH)
        fund_coin: Coin = (
            await setup_info.sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
        )[0].coin

        # Construct a spend without any time having passed
        TO_REFILL = uint64(10)
        initial_rl_puzzle: Program = construct_rate_limiting_puzzle(
            setup_info.initial_drain_date, setup_info.drain_rate, ACS
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
                            setup_info.singleton.amount,
                            setup_info.initial_drain_date,
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

        # Check that the singleton is the same puzzle hash
        new_coin: Coin = (await setup_info.sim_client.get_coin_records_by_parent_ids([setup_info.singleton.name()]))[
            0
        ].coin
        assert new_coin.puzzle_hash == setup_info.singleton.puzzle_hash
    finally:
        await setup_info.sim.close()