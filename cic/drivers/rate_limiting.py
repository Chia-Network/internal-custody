from chia.types.blockchain_format.program import Program
from chia.util.ints import uint64

from cic.load_clvm import load_clvm

RL_MOD = load_clvm("rl.clsp", package_or_requirement="cic.clsp.singleton")


def construct_rate_limiting_puzzle(
    start_date: uint64,
    start_amount: uint64,
    amount_per: uint64,
    time_interval: uint64,
    inner_puzzle: Program,
) -> Program:
    return RL_MOD.curry(
        RL_MOD.get_tree_hash(),
        start_date,
        start_amount,
        amount_per,
        time_interval,
        inner_puzzle,
    )


def solve_rate_limiting_puzzle(withdrawal_time: uint64, inner_solution: Program) -> Program:
    return Program.to(
        [
            withdrawal_time,
            inner_solution,
        ]
    )
