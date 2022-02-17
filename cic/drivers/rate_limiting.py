from chia.types.blockchain_format.program import Program
from chia.util.ints import uint64

from cic.load_clvm import load_clvm

RL_MOD = load_clvm("rl.clsp", package_or_requirement="cic.clsp")


def construct_rate_limiting_puzzle(
    start_date: uint64,
    start_amount: uint64,
    drain_rate: uint64,
    inner_puzzle: Program,
) -> Program:
    return RL_MOD.curry(
        RL_MOD.get_tree_hash(),
        start_date,
        start_amount,
        drain_rate,
        inner_puzzle,
    )


def solve_rate_limiting_puzzle(withdrawal_time: uint64, inner_solution: Program) -> Program:
    return Program.to(
        [
            withdrawal_time,
            inner_solution,
        ]
    )
