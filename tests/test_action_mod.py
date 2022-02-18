import pytest

from blspy import G2Element

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.action_coins import (
    construct_action_launcher,
    construct_action_puzzle,
    solve_action_launcher,
    solve_action_puzzle,
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
        INITIATION_PUZZLE: Program = ACS
        ACTION_INNER_PUZZLE: Program = ACS
        action_full_puzzle: Program = construct_action_puzzle(INITIATION_PUZZLE, ACTION_INNER_PUZZLE)
        INITIATION_SOLUTION: Program = Program.to([[51, action_full_puzzle.get_tree_hash(), fund_coin.amount]])

        # Generate the launch spend
        launcher_puzzle: Program = construct_action_launcher(INITIATION_PUZZLE, INITIATION_SOLUTION)
        launcher_coin = Coin(fund_coin.name(), launcher_puzzle.get_tree_hash(), fund_coin.amount)
        launch_bundle = SpendBundle(
            [
                CoinSpend(
                    fund_coin,
                    ACS,
                    Program.to([[51, launcher_puzzle.get_tree_hash(), fund_coin.amount]]),
                ),
                CoinSpend(
                    launcher_coin,
                    launcher_puzzle,
                    solve_action_launcher(),
                ),
            ],
            G2Element(),
        )

        # Process the results
        result = await sim_client.push_tx(launch_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await sim.farm_block()

        # Find the next coin
        coin_records = await sim_client.get_coin_records_by_puzzle_hashes([action_full_puzzle.get_tree_hash()])
        action_coin: Coin = coin_records[0].coin

        # Try to spend it
        action_bundle = SpendBundle(
            [
                CoinSpend(
                    action_coin,
                    action_full_puzzle,
                    solve_action_puzzle(
                        LineageProof(parent_name=launcher_coin.parent_coin_info, amount=launcher_coin.amount),
                        INITIATION_SOLUTION,
                        Program.to([]),
                    ),
                ),
            ],
            G2Element(),
        )

        # Check the output of the action coin
        action_coin_puzzle: Program = action_bundle.coin_spends[0].puzzle_reveal.to_program()
        action_coin_solution: Program = action_bundle.coin_spends[0].solution.to_program()
        assert action_coin_puzzle.run(action_coin_solution) == Program.to(
            ([71, launcher_coin.name()], [INITIATION_PUZZLE.get_tree_hash(), INITIATION_SOLUTION])
        )
    finally:
        await sim.close()
