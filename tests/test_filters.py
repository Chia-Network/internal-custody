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

from cic.drivers.drop_coins import (
    curry_rekey_puzzle,
    curry_ach_puzzle,
    calculate_ach_clawback_ph,
)
from cic.drivers.filters import (
    construct_payment_and_rekey_filter,
    construct_rekey_filter,
    solve_filter_for_payment,
    solve_filter_for_rekey,
)
from cic.drivers.merkle_utils import build_merkle_tree
from cic.drivers.prefarm import PrefarmInfo

from tests.cost_logger import CostLogger

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


@pytest.fixture(scope="module")
def cost_logger():
    return CostLogger()


@dataclasses.dataclass
class SetupInfo:
    sim: SpendSim
    sim_client: SimClient
    rnp_coin: Coin
    rko_coin: Coin
    rnp_filter: Program
    rko_filter: Program
    rekey_timelock: uint64
    starting_amount: uint64
    prefarm_info: PrefarmInfo


@pytest.fixture(scope="function")
async def _setup_info():
    sim = await SpendSim.create()
    sim_client = SimClient(sim)
    await sim.farm_block(ACS_PH)

    # Find the coins
    prefarm_coins = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH])

    # Construct our prefarm info
    REKEY_TIMELOCK = uint64(1)  # one minute
    prefarm_info = PrefarmInfo(
        bytes32([0] * 32),  # doesn't matter
        build_merkle_tree([ACS_PH])[0],
        uint64(0),  # doesn't matter
        uint64(0),  # doesn't matter
        uint64(0),  # doesn't matter
        uint64(0),  # doesn't matter
        uint64(0),  # doesn't matter
    )

    # Construct the two filters
    rnp_filter = construct_payment_and_rekey_filter(prefarm_info, prefarm_info.puzzle_root, REKEY_TIMELOCK)
    rko_filter = construct_rekey_filter(prefarm_info, prefarm_info.puzzle_root, REKEY_TIMELOCK)

    # Send coins to both of these puzzles as filters
    STARTING_AMOUNT = uint64(10000000000000000001)
    initial_bundle = SpendBundle(
        [
            CoinSpend(prefarm_coins[0].coin, ACS, Program.to([[51, rnp_filter.get_tree_hash(), STARTING_AMOUNT]])),
            CoinSpend(prefarm_coins[1].coin, ACS, Program.to([[51, rko_filter.get_tree_hash(), STARTING_AMOUNT]])),
        ],
        G2Element(),
    )
    await sim_client.push_tx(initial_bundle)
    await sim.farm_block()

    # Find the new coins
    new_coins = await sim.all_non_reward_coins()
    rnp_coin = next(coin for coin in new_coins if coin.puzzle_hash == rnp_filter.get_tree_hash())
    rko_coin = next(coin for coin in new_coins if coin.puzzle_hash == rko_filter.get_tree_hash())

    return SetupInfo(
        sim,
        sim_client,
        rnp_coin,
        rko_coin,
        rnp_filter,
        rko_filter,
        REKEY_TIMELOCK,
        STARTING_AMOUNT,
        prefarm_info,
    )


def get_proof_of_inclusion(num_puzzles: int) -> Tuple[int, List[bytes32]]:
    return build_merkle_tree([ACS_PH for i in range(0, num_puzzles)])[1][ACS_PH]


@pytest.mark.asyncio
async def test_random_create_coins_blocked(_setup_info, cost_logger):
    setup_info = await _setup_info
    try:
        rnp_bundle_even = SpendBundle(
            [
                CoinSpend(
                    setup_info.rnp_coin,
                    setup_info.rnp_filter,
                    Program.to(  # Manually construct a malicious solution
                        [
                            ACS,
                            get_proof_of_inclusion(1),
                            [
                                [[51, ACS_PH, 2]],
                                (setup_info.prefarm_info.puzzle_root, ACS_PH),
                            ],
                        ],
                    ),
                )
            ],
            G2Element(),
        )
        rko_bundle_even = SpendBundle(
            [
                CoinSpend(
                    setup_info.rko_coin,
                    setup_info.rko_filter,
                    Program.to(  # Manually construct a malicious solution
                        [
                            ACS,
                            get_proof_of_inclusion(1),
                            [
                                [[51, ACS_PH, 2]],
                                [setup_info.prefarm_info.puzzle_root, setup_info.prefarm_info.puzzle_root, uint64(0)],
                            ],
                        ],
                    ),
                )
            ],
            G2Element(),
        )
        rnp_bundle_zero = SpendBundle(
            [
                CoinSpend(
                    setup_info.rnp_coin,
                    setup_info.rnp_filter,
                    Program.to(  # Manually construct a malicious solution
                        [
                            ACS,
                            get_proof_of_inclusion(1),
                            [
                                [[51, ACS_PH, 0]],
                                [setup_info.prefarm_info.puzzle_root, setup_info.prefarm_info.puzzle_root, uint64(0)],
                            ],
                        ],
                    ),
                )
            ],
            G2Element(),
        )
        rko_bundle_zero = SpendBundle(
            [
                CoinSpend(
                    setup_info.rko_coin,
                    setup_info.rko_filter,
                    Program.to(  # Manually construct a malicious solution
                        [
                            ACS,
                            get_proof_of_inclusion(1),
                            [
                                [[51, ACS_PH, 0]],
                                [setup_info.prefarm_info.puzzle_root, setup_info.prefarm_info.puzzle_root, uint64(0)],
                            ],
                        ],
                    ),
                )
            ],
            G2Element(),
        )
        rnp_bundle_double = SpendBundle(
            [
                CoinSpend(
                    setup_info.rnp_coin,
                    setup_info.rnp_filter,
                    solve_filter_for_rekey(
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to(
                            [
                                [
                                    51,
                                    curry_rekey_puzzle(
                                        setup_info.rekey_timelock, setup_info.prefarm_info, setup_info.prefarm_info
                                    ).get_tree_hash(),
                                    0,
                                ],
                                [
                                    51,
                                    curry_rekey_puzzle(
                                        uint8(0), setup_info.prefarm_info, setup_info.prefarm_info
                                    ).get_tree_hash(),
                                    0,
                                ],
                            ]
                        ),
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.rekey_timelock,
                    ),
                )
            ],
            G2Element(),
        )
        rko_bundle_double = SpendBundle(
            [
                CoinSpend(
                    setup_info.rko_coin,
                    setup_info.rko_filter,
                    solve_filter_for_rekey(
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to(
                            [
                                [
                                    51,
                                    curry_rekey_puzzle(
                                        setup_info.rekey_timelock, setup_info.prefarm_info, setup_info.prefarm_info
                                    ).get_tree_hash(),
                                    0,
                                ],
                                [
                                    51,
                                    curry_rekey_puzzle(
                                        uint8(0), setup_info.prefarm_info, setup_info.prefarm_info
                                    ).get_tree_hash(),
                                    0,
                                ],
                            ]
                        ),
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.rekey_timelock,
                    ),
                )
            ],
            G2Element(),
        )
        for bundle in [
            rnp_bundle_even,
            rnp_bundle_zero,
            rko_bundle_even,
            rko_bundle_zero,
            rnp_bundle_double,
            rko_bundle_double,
        ]:
            result = await setup_info.sim_client.push_tx(bundle)
            assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
            with pytest.raises(ValueError, match="clvm raise"):
                bundle.coin_spends[0].puzzle_reveal.run_with_cost(INFINITE_COST, bundle.coin_spends[0].solution)
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_honest_payments(_setup_info, cost_logger):
    setup_info = await _setup_info
    try:
        REWIND_HEIGHT = setup_info.sim.block_height
        # Try initiating a payment
        payment_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.rnp_coin,
                    setup_info.rnp_filter,
                    solve_filter_for_payment(
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to([[51, curry_ach_puzzle(setup_info.prefarm_info, ACS_PH).get_tree_hash(), 2]]),
                        setup_info.prefarm_info.puzzle_root,
                        ACS_PH,  # irrelevant
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        result = await setup_info.sim_client.push_tx(payment_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        await setup_info.sim.rewind(REWIND_HEIGHT)
        cost_logger.add_cost("RNP Payment", payment_bundle)

        # Try clawing back a payment
        clawback_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.rnp_coin,
                    setup_info.rnp_filter,
                    solve_filter_for_payment(
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to([[51, calculate_ach_clawback_ph(setup_info.prefarm_info), 2]]),
                        setup_info.prefarm_info.puzzle_root,
                        bytes32([0] * 32),  # irrelevant
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        result = await setup_info.sim_client.push_tx(clawback_bundle)
        assert result[0] == MempoolInclusionStatus.SUCCESS
        await setup_info.sim.farm_block()
        cost_logger.add_cost("RNP ACH Clawback", clawback_bundle)
    finally:
        await setup_info.sim.close()


@pytest.mark.parametrize(
    "honest",
    [True, False],
)
@pytest.mark.asyncio
async def test_rekeys(_setup_info, honest, cost_logger):
    setup_info = await _setup_info
    try:
        REWIND_HEIGHT = setup_info.sim.block_height
        if honest:
            REKEY_TIMELOCK: uint8 = setup_info.rekey_timelock
        else:
            REKEY_TIMELOCK = uint8(0)
        # Try initiating a rekey with the rnp filter
        rnp_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.rnp_coin,
                    setup_info.rnp_filter,
                    solve_filter_for_rekey(
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to(
                            [
                                [
                                    51,
                                    curry_rekey_puzzle(
                                        REKEY_TIMELOCK, setup_info.prefarm_info, setup_info.prefarm_info
                                    ).get_tree_hash(),
                                    0,
                                ]
                            ]
                        ),
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.prefarm_info.puzzle_root,
                        REKEY_TIMELOCK,
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        if honest:
            result = await setup_info.sim_client.push_tx(rnp_bundle)
            assert result[0] == MempoolInclusionStatus.SUCCESS
            await setup_info.sim.farm_block()
            await setup_info.sim.rewind(REWIND_HEIGHT)
            cost_logger.add_cost("RNP Start Rekey", rnp_bundle)
        else:
            result = await setup_info.sim_client.push_tx(rnp_bundle)
            assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
            with pytest.raises(ValueError, match="clvm raise"):
                rnp_bundle.coin_spends[0].puzzle_reveal.run_with_cost(INFINITE_COST, rnp_bundle.coin_spends[0].solution)

        # Try initiating a rekey with the rko filter
        rko_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.rko_coin,
                    setup_info.rko_filter,
                    solve_filter_for_rekey(
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to(
                            [
                                [
                                    51,
                                    curry_rekey_puzzle(
                                        REKEY_TIMELOCK, setup_info.prefarm_info, setup_info.prefarm_info
                                    ).get_tree_hash(),
                                    0,
                                ]
                            ]
                        ),
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.prefarm_info.puzzle_root,
                        REKEY_TIMELOCK,
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        if honest:
            result = await setup_info.sim_client.push_tx(rko_bundle)
            assert result[0] == MempoolInclusionStatus.SUCCESS
            await setup_info.sim.farm_block()
            cost_logger.add_cost("RKO Start Rekey", rko_bundle)
        else:
            result = await setup_info.sim_client.push_tx(rko_bundle)
            assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
            with pytest.raises(ValueError, match="clvm raise"):
                rko_bundle.coin_spends[0].puzzle_reveal.run_with_cost(INFINITE_COST, rko_bundle.coin_spends[0].solution)
    finally:
        await setup_info.sim.close()


@pytest.mark.asyncio
async def test_wrong_amount_types(_setup_info, cost_logger):
    setup_info = await _setup_info
    try:
        # Try initiating a payment of 0
        bad_payment_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.rnp_coin,
                    setup_info.rnp_filter,
                    solve_filter_for_payment(
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to([[51, curry_ach_puzzle(setup_info.prefarm_info, ACS_PH).get_tree_hash(), 0]]),
                        setup_info.prefarm_info.puzzle_root,
                        ACS_PH,  # irrelevant
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        result = await setup_info.sim_client.push_tx(bad_payment_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(ValueError, match="clvm raise"):
            bad_payment_bundle.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, bad_payment_bundle.coin_spends[0].solution
            )

        # Try initiating a rekey with amount > 0 using RNP filter
        bad_rnp_rekey_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.rnp_coin,
                    setup_info.rnp_filter,
                    solve_filter_for_rekey(
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to(
                            [
                                [
                                    51,
                                    curry_rekey_puzzle(
                                        setup_info.rekey_timelock, setup_info.prefarm_info, setup_info.prefarm_info
                                    ).get_tree_hash(),
                                    2,
                                ]
                            ]
                        ),
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.rekey_timelock,
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        result = await setup_info.sim_client.push_tx(bad_rnp_rekey_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(ValueError, match="clvm raise"):
            bad_rnp_rekey_bundle.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, bad_rnp_rekey_bundle.coin_spends[0].solution
            )

        # Try initiating a rekey with amount > 0 with RKO filter
        bad_rko_rekey_bundle = SpendBundle(
            [
                CoinSpend(
                    setup_info.rko_coin,
                    setup_info.rko_filter,
                    solve_filter_for_rekey(
                        ACS,
                        get_proof_of_inclusion(1),
                        Program.to(
                            [
                                [
                                    51,
                                    curry_rekey_puzzle(
                                        setup_info.rekey_timelock, setup_info.prefarm_info, setup_info.prefarm_info
                                    ).get_tree_hash(),
                                    2,
                                ]
                            ]
                        ),
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.prefarm_info.puzzle_root,
                        setup_info.rekey_timelock,
                    ),
                )
            ],
            G2Element(),
        )
        # Process the spend
        result = await setup_info.sim_client.push_tx(bad_rko_rekey_bundle)
        assert result == (MempoolInclusionStatus.FAILED, Err.GENERATOR_RUNTIME_ERROR)
        with pytest.raises(ValueError, match="clvm raise"):
            bad_rko_rekey_bundle.coin_spends[0].puzzle_reveal.run_with_cost(
                INFINITE_COST, bad_rko_rekey_bundle.coin_spends[0].solution
            )
    finally:
        await setup_info.sim.close()


def test_cost(cost_logger):
    cost_logger.log_cost_statistics()
