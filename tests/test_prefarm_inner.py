import dataclasses
import pytest

from blspy import G2Element
from typing import Tuple, List

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program, INFINITE_COST
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.util.errors import Err
from chia.util.ints import uint8, uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH

from cic.drivers.drop_coins import (
    curry_rekey_puzzle,
    curry_ach_puzzle,
    solve_rekey_completion,
)
from cic.drivers.merkle_utils import build_merkle_tree
from cic.drivers.prefarm import (
    construct_prefarm_inner_puzzle,
    solve_prefarm_inner,
    SpendType,
)
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.singleton import (
    construct_singleton,
    solve_singleton,
    generate_launch_conditions_and_coin_spend,
    construct_p2_singleton,
    solve_p2_singleton,
)

from tests.cost_logger import CostLogger

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()
SECONDS_IN_A_DAY = 86400


@pytest.fixture(scope="module")
def cost_logger():
    return CostLogger()


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
async def _setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)

    # Define constants
    PAYMENT_CLAWBACK_PERIOD = uint64(0)  # pointless for this test
    REKEY_CLAWBACK_PERIOD = uint64(0)  # pointless for this test
    WITHDRAWAL_TIMELOCK = uint64(60)
    REKEY_INCREMENTS = uint64(15)
    SLOW_REKEY_TIMELOCK = uint64(15)
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
        build_merkle_tree(PUZZLE_HASHES)[0],  # puzzle_root: bytes32
        WITHDRAWAL_TIMELOCK,  # withdrawal_timelock: uint64
        PAYMENT_CLAWBACK_PERIOD,  # payment_clawback_period: uint64
        REKEY_CLAWBACK_PERIOD,  # rekey_clawback_period: uint64
        REKEY_INCREMENTS,  # rekey_increments: uint64
        SLOW_REKEY_TIMELOCK,  # slow_rekey_timelock: uint64
    )
    conditions, launch_spend = generate_launch_conditions_and_coin_spend(
        big_coin, construct_prefarm_inner_puzzle(prefarm_info), starting_amount
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
async def test_rekey(_setup_info, cost_logger):
    setup_info = await _setup_info
    try:
        TIMELOCK = uint8(1)  # one minute
        new_prefarm_info: PrefarmInfo = dataclasses.replace(
            setup_info.prefarm_info,
            puzzle_root=build_merkle_tree([ACS_PH, ACS_PH])[0],
        )

        rekey_puzzle: Program = curry_rekey_puzzle(TIMELOCK, setup_info.prefarm_info, new_prefarm_info)

        prefarm_inner_puzzle: Program = construct_prefarm_inner_puzzle(setup_info.prefarm_info)

        # Parameterize this spend and test that it can't make the malicious announcement
        def start_rekey_spend(malicious: bool) -> SpendBundle:
            if malicious:
                malicious_condition = [[62, "r"]]
            else:
                malicious_condition = []
            return SpendBundle(
                [
                    CoinSpend(
                        setup_info.singleton,
                        construct_singleton(
                            setup_info.launcher_id,
                            prefarm_inner_puzzle,
                        ),
                        solve_singleton(
                            setup_info.first_lineage_proof,
                            setup_info.singleton.amount,
                            solve_prefarm_inner(
                                SpendType.START_REKEY,
                                setup_info.singleton.amount,
                                timelock=TIMELOCK,
                                puzzle_reveal=ACS,
                                proof_of_inclusion=get_proof_of_inclusion(1),
                                puzzle_root=new_prefarm_info.puzzle_root,
                                puzzle_solution=[
                                    [
                                        51,
                                        prefarm_inner_puzzle.get_tree_hash(),
                                        setup_info.singleton.amount,
                                    ],
                                    [51, rekey_puzzle.get_tree_hash(), 0],
                                    *malicious_condition,
                                ],
                            ),
                        ),
                    )
                ],
                G2Element(),
            )

        # Test the malicious spend fails first
        malicious_rekey_spend: SpendBundle = start_rekey_spend(True)
        result = await setup_info.sim_client.push_tx(malicious_rekey_spend)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(ValueError, match="clvm raise"):
            malicious_rekey_spend.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, malicious_rekey_spend.coin_spends[0].solution
            )
        # Process results
        honest_rekey_spend: SpendBundle = start_rekey_spend(False)
        result = await setup_info.sim_client.push_tx(honest_rekey_spend)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)
        setup_info.sim.pass_time(TIMELOCK * setup_info.prefarm_info.rekey_increments)
        await setup_info.sim.farm_block()

        result = await setup_info.sim_client.push_tx(honest_rekey_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Start Rekey", honest_rekey_spend)

        # Find the rekey coin and the new singleton
        rekey_coin: Coin = (
            await setup_info.sim_client.get_coin_records_by_puzzle_hashes([rekey_puzzle.get_tree_hash()])
        )[0].coin
        new_singleton: Coin = (
            await setup_info.sim_client.get_coin_records_by_puzzle_hashes(
                [setup_info.singleton.puzzle_hash], include_spent_coins=False
            )
        )[0].coin

        # Finish the rekey
        new_singleton_lineage_proof = LineageProof(
            setup_info.singleton.parent_coin_info,
            prefarm_inner_puzzle.get_tree_hash(),
            setup_info.singleton.amount,
        )
        finish_rekey_spend = SpendBundle(
            [
                CoinSpend(
                    new_singleton,
                    construct_singleton(
                        setup_info.launcher_id,
                        prefarm_inner_puzzle,
                    ),
                    solve_singleton(
                        new_singleton_lineage_proof,
                        setup_info.singleton.amount,
                        solve_prefarm_inner(
                            SpendType.FINISH_REKEY,
                            setup_info.singleton.amount,
                            timelock=TIMELOCK,
                            puzzle_root=new_prefarm_info.puzzle_root,
                        ),
                    ),
                ),
                CoinSpend(
                    rekey_coin,
                    rekey_puzzle,
                    solve_rekey_completion(setup_info.prefarm_info, new_singleton_lineage_proof),
                ),
            ],
            G2Element(),
        )
        # Process results
        setup_info.sim.pass_time(TIMELOCK)
        await setup_info.sim.farm_block()
        result = await setup_info.sim_client.push_tx(finish_rekey_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Finish Rekey", finish_rekey_spend)

        # Check the singleton is at the new state
        new_singleton_puzzle: Program = construct_singleton(
            setup_info.launcher_id, construct_prefarm_inner_puzzle(new_prefarm_info)
        )
        assert (
            len(await setup_info.sim_client.get_coin_records_by_puzzle_hashes([new_singleton_puzzle.get_tree_hash()]))
            == 1
        )
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_payments(_setup_info, cost_logger):
    setup_info = await _setup_info
    try:
        WITHDRAWAL_AMOUNT = uint64(500)
        prefarm_inner_puzzle: Program = construct_prefarm_inner_puzzle(setup_info.prefarm_info)
        for honest in (True, False):
            withdrawal_spend = SpendBundle(
                [
                    CoinSpend(
                        setup_info.singleton,
                        construct_singleton(
                            setup_info.launcher_id,
                            prefarm_inner_puzzle,
                        ),
                        solve_singleton(
                            setup_info.first_lineage_proof,
                            setup_info.singleton.amount,
                            solve_prefarm_inner(
                                SpendType.HANDLE_PAYMENT,
                                setup_info.singleton.amount,
                                puzzle_reveal=ACS,
                                proof_of_inclusion=get_proof_of_inclusion(1),
                                out_amount=WITHDRAWAL_AMOUNT,
                                in_amount=uint64(0) if honest else -1,
                                p2_ph=ACS_PH,
                                puzzle_solution=[
                                    [
                                        51,
                                        prefarm_inner_puzzle.get_tree_hash(),
                                        setup_info.singleton.amount - WITHDRAWAL_AMOUNT - (0 if honest else 1),
                                    ],
                                    [
                                        51,
                                        curry_ach_puzzle(setup_info.prefarm_info, ACS_PH).get_tree_hash(),
                                        WITHDRAWAL_AMOUNT,
                                    ],
                                ],
                            ),
                        ),
                    )
                ],
                G2Element(),
            )
            if honest:
                # Process results
                result = await setup_info.sim_client.push_tx(withdrawal_spend)
                assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)
                setup_info.sim.pass_time(setup_info.prefarm_info.withdrawal_timelock)
                await setup_info.sim.farm_block()

                result = await setup_info.sim_client.push_tx(withdrawal_spend)
                assert result[0] == MempoolInclusionStatus.SUCCESS
                await setup_info.sim.farm_block()
                cost_logger.add_cost("Withdrawal", withdrawal_spend)
            else:
                with pytest.raises(ValueError, match="clvm raise"):
                    spend: CoinSpend = withdrawal_spend.coin_spends[0]
                    puzzle: Program = spend.puzzle_reveal.to_program()
                    solution: Program = spend.solution.to_program()
                    puzzle.run(solution)

        # Find the new singleton
        new_singleton: Coin = (
            await setup_info.sim_client.get_coin_records_by_puzzle_hashes(
                [setup_info.singleton.puzzle_hash], include_spent_coins=False
            )
        )[0].coin

        # Accept the p2 singleton we made in the setup
        accept_spend = SpendBundle(
            [
                CoinSpend(
                    new_singleton,
                    construct_singleton(
                        setup_info.launcher_id,
                        prefarm_inner_puzzle,
                    ),
                    solve_singleton(
                        LineageProof(
                            setup_info.singleton.parent_coin_info,
                            prefarm_inner_puzzle.get_tree_hash(),
                            setup_info.singleton.amount,
                        ),
                        setup_info.singleton.amount - WITHDRAWAL_AMOUNT,
                        solve_prefarm_inner(
                            SpendType.HANDLE_PAYMENT,
                            setup_info.singleton.amount - WITHDRAWAL_AMOUNT,
                            puzzle_reveal=ACS,
                            proof_of_inclusion=get_proof_of_inclusion(1),
                            out_amount=uint64(0),
                            in_amount=setup_info.p2_singleton.amount,
                            p2_ph=ACS_PH,
                            # create a puzzle announcement for the p2_singleton to assert
                            puzzle_solution=[
                                [
                                    51,
                                    prefarm_inner_puzzle.get_tree_hash(),
                                    new_singleton.amount + setup_info.p2_singleton.amount,
                                ],
                                [62, setup_info.p2_singleton.name()],
                            ],
                        ),
                    ),
                ),
                CoinSpend(
                    setup_info.p2_singleton,
                    construct_p2_singleton(setup_info.launcher_id),
                    solve_p2_singleton(setup_info.p2_singleton, prefarm_inner_puzzle.get_tree_hash()),
                ),
            ],
            G2Element(),
        )
        # Process results
        result = await setup_info.sim_client.push_tx(accept_spend)
        assert result == (MempoolInclusionStatus.FAILED, Err.ASSERT_SECONDS_RELATIVE_FAILED)
        setup_info.sim.pass_time(setup_info.prefarm_info.withdrawal_timelock)
        await setup_info.sim.farm_block()

        result = await setup_info.sim_client.push_tx(accept_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("Accept Payment", accept_spend)

        # Find the new singleton and assert it's amount
        final_singleton: Coin = (
            await setup_info.sim_client.get_coin_records_by_puzzle_hashes(
                [setup_info.singleton.puzzle_hash], include_spent_coins=False
            )
        )[0].coin
        assert (
            final_singleton.amount == setup_info.singleton.amount - WITHDRAWAL_AMOUNT + setup_info.p2_singleton.amount
        )
    finally:
        await setup_info.sim.close()


def test_cost(cost_logger):
    cost_logger.log_cost_statistics()
