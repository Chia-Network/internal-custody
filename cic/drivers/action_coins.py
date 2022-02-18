from typing import List, Tuple

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend
from chia.types.condition_opcodes import ConditionOpcode
from chia.util.hash import std_hash
from chia.util.ints import uint64
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.payment import Payment

from cic.load_clvm import load_clvm

STATEFUL_LAUNCHER_MOD = load_clvm("stateful_launcher.clsp", package_or_requirement="cic.clsp")
ACTION_MOD = load_clvm("action_coin.clsp", package_or_requirement="cic.clsp")


def construct_stateful_launcher(
    additional_conditions: List[Program],
    additional_state: Program,
) -> Program:
    return STATEFUL_LAUNCHER_MOD.curry(
        Program.to(additional_conditions),
        additional_state,
    )


def construct_action_coin(
    inner_puzzle: Program,
) -> Program:
    return ACTION_MOD.curry(
        STATEFUL_LAUNCHER_MOD.get_tree_hash(),
        inner_puzzle,
    )


def solve_stateful_launcher(
    payment: Payment,
) -> Program:
    return Program.to([payment.as_condition_args()])


def solve_action_coin(
    additional_conditions: List[Program],
    additional_state: Program,
    launcher_lineage_proof: LineageProof,
    inner_solution: Program,
) -> Program:
    return Program.to(
        [
            Program.to([*additional_conditions]),
            additional_state,
            launcher_lineage_proof.to_program(),
            inner_solution,
        ]
    )


def generate_stateful_launch_conditions_and_coin_spend(
    fund_coin: Coin,
    additional_conditions: List[Program],
    additional_state: Program,
    action_inner_puzzle: Program,
    launch_amount: uint64,
    launch_memos: List[bytes] = [],
) -> Tuple[List[Program], CoinSpend]:
    launcher_puzzle: Program = construct_stateful_launcher(additional_conditions, additional_state)
    launcher_coin = Coin(fund_coin.name(), launcher_puzzle.get_tree_hash(), launch_amount)
    curried_action_coin: Program = construct_action_coin(action_inner_puzzle)

    action_payment = Payment(curried_action_coin.get_tree_hash(), launch_amount, launch_memos)
    launcher_solution: Program = solve_stateful_launcher(action_payment)
    create_launcher = Program.to(
        [
            ConditionOpcode.CREATE_COIN,
            launcher_puzzle.get_tree_hash(),
            launch_amount,
        ],
    )
    assert_launcher_announcement = Program.to(
        [
            ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT,
            std_hash(launcher_coin.name() + Program.to(action_payment.as_condition_args()).get_tree_hash()),
        ],
    )

    conditions: List[Program] = [create_launcher, assert_launcher_announcement]

    launcher_coin_spend = CoinSpend(
        launcher_coin,
        launcher_puzzle,
        launcher_solution,
    )

    return conditions, launcher_coin_spend
