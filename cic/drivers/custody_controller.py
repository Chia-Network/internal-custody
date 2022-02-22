from chia.types.announcement import Announcement
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64

from cic.load_clvm import load_clvm

CUSTODY_CONTROLLER = load_clvm("singleton_custody_controller.clsp", package_or_requirement="cic.clsp")


def construct_custody_puzzle(actions_root: bytes32, state: Program) -> Program:
    return CUSTODY_CONTROLLER.curry(
        CUSTODY_CONTROLLER.get_tree_hash(),
        actions_root,
        state,
    )


def solve_custody_puzzle(
    amount: uint64,
    action_ph: bytes32,
    proof_of_inclusion: Program,
    new_actions_root: bytes32,
    new_state: Program,
    new_amount: uint64,
) -> Program:
    return Program.to(
        [
            amount,
            action_ph,
            proof_of_inclusion,
            new_actions_root,
            new_state,
            new_amount,
        ]
    )


def calculate_communication_announcement(
    puzzle_hash: bytes32,
    action_root: bytes32,
    state: Program,
    amount: uint64,
) -> Announcement:
    return Announcement(
        puzzle_hash,
        Program.to([action_root, state, amount]).get_tree_hash(),
    )
