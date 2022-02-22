import math
import pytest

from blspy import G2Element
from clvm.EvalError import EvalError
from dataclasses import dataclass
from typing import List, Dict, Tuple

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.custody_controller import (
    construct_custody_puzzle,
    solve_custody_puzzle,
    calculate_communication_announcement,
)
from cic.drivers.merkle_utils import build_merkle_tree
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
    initial_actions: List[bytes32]
    initial_action_root: bytes32
    initial_action_proofs: Dict[bytes32, Tuple[int, List[bytes32]]]
    initial_state: Program


@pytest.fixture(scope="function")
async def setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)

    # Define constants
    INITIAL_ACTIONS = [ACS_PH]
    INITIAL_STATE = Program.to([b"\xab", b"\xcd", b"\xef"])
    initial_actions_root, initial_actions_proofs = build_merkle_tree(INITIAL_ACTIONS)

    # Identify the coin
    prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])
    coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 18375000000000000000)

    # Launch it to the starting state
    starting_amount = 18374999999999999999
    conditions, launch_spend = generate_launch_conditions_and_coin_spend(
        coin, construct_custody_puzzle(initial_actions_root, INITIAL_STATE), 18374999999999999999
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
        INITIAL_ACTIONS,
        initial_actions_root,
        initial_actions_proofs,
        INITIAL_STATE,
    )


@pytest.mark.asyncio
async def test_singleton_innerpuz(setup_info):
    try:
        action_coin: Coin = (await setup_info.sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False))[0].coin
        new_amount: uint64 = setup_info.singleton.amount - 10
        drain_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.singleton,
                    construct_singleton(
                        setup_info.launcher_id,
                        construct_custody_puzzle(setup_info.initial_action_root, setup_info.initial_state),
                    ),
                    solve_singleton(
                        setup_info.first_lineage_proof,
                        setup_info.singleton.amount,
                        solve_custody_puzzle(
                            setup_info.singleton.amount,  # this coin amount
                            ACS_PH,  # The PH of the action coin
                            setup_info.initial_action_proofs[ACS_PH],  # The proof of the PH in the action root
                            setup_info.initial_action_root,  # We're setting the same action root...
                            setup_info.initial_state,  # ...and the same state...
                            new_amount,  # ...but we're going to drain it a bit
                        ),
                    ),
                ),
                CoinSpend(
                    action_coin,
                    ACS,
                    Program.to(
                        [
                            [
                                62,
                                calculate_communication_announcement(
                                    ACS_PH,
                                    setup_info.initial_action_root,
                                    setup_info.initial_state,
                                    new_amount,
                                ).message,
                            ],
                            [
                                63,
                                calculate_communication_announcement(
                                    setup_info.singleton.puzzle_hash,
                                    setup_info.initial_action_root,
                                    setup_info.initial_state,
                                    setup_info.singleton.amount,
                                ).name(),
                            ],
                        ]
                    ),
                ),
            ],
            G2Element(),
        )

        # Process the results
        result = await setup_info.sim_client.push_tx(drain_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

        # Construct what the coin should be a fetch it
        new_singleton = Coin(setup_info.singleton.name(), setup_info.singleton.puzzle_hash, new_amount)
        assert await setup_info.sim_client.get_coin_record_by_name(new_singleton.name()) is not None

    finally:
        await setup_info.sim.close()
