import dataclasses
import pytest

from blspy import G2Element
from clvm.EvalError import EvalError
from typing import Tuple, List

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program, INFINITE_COST
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.util.errors import Err
from chia.util.ints import uint32, uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH

from cic.drivers.drop_coins import (
    curry_rekey_puzzle,
    curry_ach_puzzle,
    solve_rekey_completion,
    solve_rekey_clawback,
    solve_ach_completion,
    solve_ach_clawback,
    calculate_ach_clawback_ph,
)
from cic.drivers.merkle_utils import build_merkle_tree
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.singleton import (
    construct_singleton,
    solve_singleton,
    generate_launch_conditions_and_coin_spend,
    construct_p2_singleton,
)

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


@dataclasses.dataclass
class SetupInfo:
    sim: SpendSim
    sim_client: SimClient
    singleton: Coin
    p2_singleton: Coin
    launcher_id: bytes32
    first_lineage_proof: LineageProof
    prefarm_info: PrefarmInfo


@pytest.fixture(scope="function")
async def setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)

    # Define constants
    START_DATE = uint64(0)  # pointless for this test
    DRAIN_RATE = uint64(0)  # pointless for this test
    WITHDRAWAL_TIMELOCK = uint64(0)  # pointless for this test
    CLAWBACK_PERIOD = uint64(60)
    PUZZLE_HASHES = [ACS_PH]

    # Identify the prefarm coins
    prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])
    big_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 18375000000000000000)
    small_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 2625000000000000000)

    # Launch them to their starting state
    starting_amount = 18374999999999999999
    launcher_coin = Coin(big_coin.name(), SINGLETON_LAUNCHER_HASH, starting_amount)
    prefarm_info = PrefarmInfo(
        launcher_coin.name(),  # launcher_id: bytes32
        START_DATE,  # start_date: uint64
        starting_amount,  # starting_amount: uint64
        DRAIN_RATE,  # mojos_per_second: uint64
        PUZZLE_HASHES,  # puzzle_hash_list: List[bytes32]
        WITHDRAWAL_TIMELOCK, # withdrawal_timelock: uint64
        CLAWBACK_PERIOD,  # clawback_period: uint64
    )
    conditions, launch_spend = generate_launch_conditions_and_coin_spend(big_coin, ACS, starting_amount)
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
                Program.to([[51, construct_p2_singleton(launch_spend.coin.name()).get_tree_hash(), 1000]]),
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
    small_coin = next(coin for coin in prefarm_coins if coin.amount == 1000)

    return SetupInfo(
        sim,
        sim_client,
        big_coin,
        small_coin,
        launch_spend.coin.name(),
        LineageProof(parent_name=launch_spend.coin.parent_coin_info, amount=launch_spend.coin.amount),
        prefarm_info,
    )


def get_proof_of_inclusion(num_puzzles: int) -> Tuple[int, List[bytes32]]:
    return build_merkle_tree([ACS_PH for i in range(0, num_puzzles)])[1][ACS_PH]


@pytest.mark.asyncio
async def test_ach(setup_info):
    try:
        PAYMENT_AMOUNT = uint64(1024)
        ach_puzzle: Program = curry_ach_puzzle(setup_info.prefarm_info, ACS_PH)
        create_ach_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.singleton,
                    construct_singleton(
                        setup_info.launcher_id,
                        ACS,
                    ),
                    solve_singleton(
                        setup_info.first_lineage_proof,
                        setup_info.singleton.amount,
                        Program.to(
                            [
                                [51, ach_puzzle.get_tree_hash(), PAYMENT_AMOUNT],
                                [51, setup_info.singleton.puzzle_hash, setup_info.singleton.amount - PAYMENT_AMOUNT],
                            ]
                        ),
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        result = await setup_info.sim_client.push_tx(create_ach_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

        # Find the new coin
        ach_coin = Coin(setup_info.singleton.name(), ach_puzzle.get_tree_hash(), PAYMENT_AMOUNT)

        # Attempt to clawback the ACH coin
        clawback_ach_bundle = SpendBundle(
            [
                CoinSpend(
                    ach_coin,
                    ach_puzzle,
                    solve_ach_clawback(
                        setup_info.prefarm_info,
                        PAYMENT_AMOUNT,
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to([[51, calculate_ach_clawback_ph(setup_info.prefarm_info), PAYMENT_AMOUNT]]),
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        result = await setup_info.sim_client.push_tx(clawback_ach_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        rewind_here: uint32 = setup_info.sim.block_height
        await setup_info.sim.farm_block()

        # Rewind and try to claw it forward
        await setup_info.sim.rewind(rewind_here)
        forward_ach_bundle = SpendBundle(
            [
                CoinSpend(
                    ach_coin,
                    ach_puzzle,
                    solve_ach_completion(
                        setup_info.prefarm_info,
                        PAYMENT_AMOUNT,
                    ),
                )
            ],
            G2Element(),
        )
        # Process spend
        result = await setup_info.sim_client.push_tx(forward_ach_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)
        setup_info.sim.pass_time(setup_info.prefarm_info.clawback_period)
        await setup_info.sim.farm_block()

        result = await setup_info.sim_client.push_tx(forward_ach_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_rekey(setup_info):
    try:
        REKEY_TIMELOCK = uint64(60)
        new_prefarm_info: PrefarmInfo = dataclasses.replace(setup_info.prefarm_info, puzzle_hash_list=[ACS_PH, ACS_PH])
        rekey_puzzle: Program = curry_rekey_puzzle(REKEY_TIMELOCK, setup_info.prefarm_info, new_prefarm_info)
        create_rekey_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.singleton,
                    construct_singleton(
                        setup_info.launcher_id,
                        ACS,
                    ),
                    solve_singleton(
                        setup_info.first_lineage_proof,
                        setup_info.singleton.amount,
                        Program.to(
                            [
                                [51, rekey_puzzle.get_tree_hash(), 0],
                                [51, ACS_PH, setup_info.singleton.amount],
                            ]
                        ),
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        result = await setup_info.sim_client.push_tx(create_rekey_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

        # Find the new coins
        rekey_coin = Coin(setup_info.singleton.name(), rekey_puzzle.get_tree_hash(), 0)
        new_singleton = Coin(setup_info.singleton.name(), setup_info.singleton.puzzle_hash, setup_info.singleton.amount)

        # Attempt to clawback the ACH coin
        # First, let's try to sneakily complete the rekey and make sure it fails
        malicious_rekey_bundle = SpendBundle(
            [
                CoinSpend(
                    rekey_coin,
                    rekey_puzzle,
                    solve_rekey_clawback(
                        setup_info.prefarm_info,
                        rekey_puzzle.get_tree_hash(),
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to(
                            [
                                [51, rekey_puzzle.get_tree_hash(), 0],
                                [62, 'rekey'],
                            ]
                        ),
                    ),
                )
            ],
            G2Element(),
        )
        result = await setup_info.sim_client.push_tx(malicious_rekey_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(EvalError, match="clvm raise"):
            malicious_rekey_bundle.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, malicious_rekey_bundle.coin_spends[0].solution
            )
        # Then, let's do it honestly
        clawback_rekey_bundle = SpendBundle(
            [
                CoinSpend(
                    rekey_coin,
                    rekey_puzzle,
                    solve_rekey_clawback(
                        setup_info.prefarm_info,
                        rekey_puzzle.get_tree_hash(),
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to([[51, rekey_puzzle.get_tree_hash(), 0]]),
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        result = await setup_info.sim_client.push_tx(clawback_rekey_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        rewind_here: uint32 = setup_info.sim.block_height
        await setup_info.sim.farm_block()

        # Rewind and try to claw it forward
        await setup_info.sim.rewind(rewind_here)
        forward_rekey_bundle = SpendBundle(
            [
                CoinSpend(
                    rekey_coin,
                    rekey_puzzle,
                    solve_rekey_completion(
                        setup_info.prefarm_info,
                        LineageProof(setup_info.singleton.parent_coin_info, ACS_PH, setup_info.singleton.amount),
                    ),
                ),
                CoinSpend(
                    new_singleton,
                    construct_singleton(
                        setup_info.launcher_id,
                        ACS,
                    ),
                    solve_singleton(
                        LineageProof(setup_info.singleton.parent_coin_info, ACS_PH, setup_info.singleton.amount),
                        setup_info.singleton.amount,
                        Program.to(
                            [
                                [62, "rekey"],
                                [51, ACS_PH, setup_info.singleton.amount],
                            ]
                        ),
                    ),
                ),
            ],
            G2Element(),
        )
        # Process spend
        result = await setup_info.sim_client.push_tx(forward_rekey_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)
        setup_info.sim.pass_time(REKEY_TIMELOCK)
        await setup_info.sim.farm_block()

        result = await setup_info.sim_client.push_tx(forward_rekey_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

    finally:
        await setup_info.sim.close()
