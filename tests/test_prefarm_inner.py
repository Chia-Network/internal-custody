import dataclasses
import pytest

from blspy import G2Element
from typing import Tuple, List

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH

from cic.drivers.merkle_utils import build_merkle_tree
from cic.drivers.prefarm import (
    construct_prefarm_inner_puzzle,
    curry_rekey_puzzle,
    curry_ach_puzzle,
    solve_prefarm_inner,
    solve_rekey_completion,
    PrefarmInfo,
    SpendType,
)
from cic.drivers.singleton import (
    construct_singleton,
    solve_singleton,
    generate_launch_conditions_and_coin_spend,
    construct_p2_singleton,
    solve_p2_singleton,
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
    PUZZLE_HASHES = [ACS_PH]

    # Identify the prefarm coins
    prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])
    big_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 18375000000000000000)
    small_coin = next(cr.coin for cr in prefarm_coins if cr.coin.amount == 2625000000000000000)

    # Launch them to their starting state
    starting_amount = 18374999999999999999
    launcher_coin = Coin(big_coin.name(), SINGLETON_LAUNCHER_HASH, starting_amount)
    prefarm_info = PrefarmInfo(
        launcher_coin.name(),  # launcher_id
        START_DATE,  # start_date: uint64
        starting_amount,  # starting_amount: uint64
        DRAIN_RATE,  # mojos_per_second: uint64
        PUZZLE_HASHES,  # puzzle_hash_list: List[bytes32]
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
async def test_rekey(setup_info):
    try:
        new_prefarm_info: PrefarmInfo = dataclasses.replace(
            setup_info.prefarm_info,
            puzzle_hash_list=[ACS_PH, ACS_PH],
        )

        rekey_puzzle: Program = curry_rekey_puzzle(setup_info.prefarm_info, new_prefarm_info)

        prefarm_inner_puzzle: Program = construct_prefarm_inner_puzzle(setup_info.prefarm_info)
        start_rekey_spend = SpendBundle(
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
                            puzzle_reveal=ACS,
                            proof_of_inclusion=get_proof_of_inclusion(1),
                            puzzle_hash_list=new_prefarm_info.puzzle_hash_list,
                            puzzle_solution=[[51, rekey_puzzle.get_tree_hash(), 0]],
                        ),
                    ),
                )
            ],
            G2Element(),
        )
        # Process results
        result = await setup_info.sim_client.push_tx(start_rekey_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

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
        finish_rekey_spend = SpendBundle(
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
                        setup_info.singleton.amount,
                        solve_prefarm_inner(
                            SpendType.FINISH_REKEY,
                            setup_info.singleton.amount,
                            puzzle_hash_list=new_prefarm_info.puzzle_hash_list,
                        ),
                    ),
                ),
                CoinSpend(
                    rekey_coin,
                    rekey_puzzle,
                    solve_rekey_completion(new_prefarm_info),
                ),
            ],
            G2Element(),
        )
        # Process results
        result = await setup_info.sim_client.push_tx(finish_rekey_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

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
async def test_payments(setup_info):
    try:
        WITHDRAWAL_AMOUNT = uint64(500)
        prefarm_inner_puzzle: Program = construct_prefarm_inner_puzzle(setup_info.prefarm_info)
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
                            SpendType.WITHDRAW_PAYMENT,
                            setup_info.singleton.amount,
                            puzzle_reveal=ACS,
                            proof_of_inclusion=get_proof_of_inclusion(1),
                            withdrawal_amount=WITHDRAWAL_AMOUNT,
                            p2_ph=ACS_PH,
                            puzzle_solution=[
                                [
                                    51,
                                    curry_ach_puzzle(setup_info.prefarm_info, ACS_PH).get_tree_hash(),
                                    WITHDRAWAL_AMOUNT,
                                ]
                            ],
                        ),
                    ),
                )
            ],
            G2Element(),
        )
        # Process results
        result = await setup_info.sim_client.push_tx(withdrawal_spend)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

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
                            SpendType.ACCEPT_PAYMENT,
                            setup_info.singleton.amount - WITHDRAWAL_AMOUNT,
                            p2_singleton_lineage_proof=LineageProof(
                                parent_name=setup_info.p2_singleton.parent_coin_info,
                                amount=setup_info.p2_singleton.amount,
                            ),
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
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()

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
