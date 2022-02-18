import pytest

from typing import List

from blspy import G2Element

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.action_coins import (
    generate_stateful_launch_conditions_and_coin_spend,
    construct_action_coin,
    solve_action_coin,
)

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


@pytest.mark.asyncio
async def test_setup():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)
    try:
        # Find the coin
        coin_records = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])
        fund_coin: Coin = coin_records[0].coin

        # Define our action's state
        ADDITIONAL_CONDITIONS: List[Program] = [Program.to([60, b"\xdeadbeef"])]
        ADDITIONAL_STATE: Program = Program.to([b"\xcafef00d", b"\xf00dcafe", b"\xf00d"])

        # Generate the launch spend
        launch_conditions, launch_spend = generate_stateful_launch_conditions_and_coin_spend(
            fund_coin,
            ADDITIONAL_CONDITIONS,
            ADDITIONAL_STATE,
            Program.to([]),
            fund_coin.amount,
        )
        launch_bundle = SpendBundle(
            [
                CoinSpend(
                    fund_coin,
                    ACS,
                    Program.to([*launch_conditions]),
                ),
                launch_spend,
            ],
            G2Element(),
        )

        # Process the results
        result = await sim_client.push_tx(launch_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await sim.farm_block()

        # Find the next coin
        action_puzzle: Program = construct_action_coin(Program.to([]))
        coin_records = await sim_client.get_coin_records_by_puzzle_hashes([action_puzzle.get_tree_hash()])
        action_coin: Coin = coin_records[0].coin

        # Try to spend it
        action_bundle = SpendBundle(
            [
                CoinSpend(
                    action_coin,
                    action_puzzle,
                    solve_action_coin(
                        ADDITIONAL_CONDITIONS,
                        ADDITIONAL_STATE,
                        LineageProof(parent_name=launch_spend.coin.parent_coin_info, amount=launch_spend.coin.amount),
                        Program.to([]),
                    ),
                ),
            ],
            G2Element(),
        )

        # Process the results
        result = await sim_client.push_tx(action_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await sim.farm_block()
    finally:
        await sim.close()
