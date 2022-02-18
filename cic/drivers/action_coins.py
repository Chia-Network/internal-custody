from chia.types.blockchain_format.program import Program
from chia.wallet.lineage_proof import LineageProof

from cic.load_clvm import load_clvm

ACTION_LAUNCHER_MOD = load_clvm("action_launcher.clsp", package_or_requirement="cic.clsp")
ACTION_MOD = load_clvm("action_coin.clsp", package_or_requirement="cic.clsp")


def construct_action_launcher(
    initiation_puzzle: Program,
    initiation_solution: Program,
) -> Program:
    return ACTION_LAUNCHER_MOD.curry(
        initiation_puzzle,
        initiation_solution,
    )


def construct_action_puzzle(
    initiation_puzzle: Program,
    inner_puzzle: Program,
) -> Program:
    return ACTION_MOD.curry(
        ACTION_LAUNCHER_MOD.get_tree_hash(),
        initiation_puzzle.get_tree_hash(),
        inner_puzzle,
    )


def solve_action_launcher() -> Program:
    return Program.to([])


def solve_action_puzzle(
    launcher_lineage_proof: LineageProof,
    initiation_solution: Program,
    inner_solution: Program,
) -> Program:
    return Program.to(
        [
            launcher_lineage_proof.to_program(),
            initiation_solution,
            inner_solution,
        ]
    )
