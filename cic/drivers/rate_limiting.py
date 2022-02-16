from typing import List, Tuple

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.condition_opcodes import ConditionOpcode
from chia.util.hash import std_hash
from chia.util.ints import uint64
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH

from cic.load_clvm import load_clvm

RL_MOD = load_clvm("rl.clsp", package_or_requirement="cic.clsp")


def construct_rate_limiting_puzzle(initial_drain_date: uint64, drain_rate: uint64, inner_puzzle: Program) -> Program:
    return RL_MOD.curry(
        RL_MOD.get_tree_hash(),
        drain_rate,
        initial_drain_date,
        inner_puzzle,
    )


def solve_rate_limiting_puzzle(coin_amount: uint64, withdrawal_time: uint64, inner_solution: Program) -> Program:
    return Program.to(
        [
            withdrawal_time,
            amount,
            inner_solution,
        ]
    )
