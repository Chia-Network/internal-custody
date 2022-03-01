import enum

from dataclasses import dataclass
from typing import List

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64

from cic.drivers.merkle_utils import build_merkle_tree
from cic.drivers.singleton import construct_singleton, construct_p2_singleton
from cic.drivers.rate_limiting import construct_rate_limiting_puzzle
from cic.load_clvm import load_clvm


PREFARM_INNER = load_clvm("prefarm_inner.clsp", package_or_requirement="cic.clsp")
# mock: (mod (mod_hash puz_root new_root) (list (list 62 new_root)))
REKEY_MOD = Program.fromhex("ff04ffff04ffff013effff04ff0bff808080ff8080")
ACH_MOD = Program.to(1)  # mock
SLOW_REKEY_MOD = REKEY_MOD  # mock


@dataclass
class PrefarmInfo:
    launcher_id: bytes32
    start_date: uint64
    starting_amount: uint64
    mojos_per_second: uint64
    puzzle_hash_list: List[bytes32]


class SpendType(int, enum.Enum):
    FINISH_REKEY = 1
    LOCK = 2
    START_REKEY = 3
    WITHDRAW_PAYMENT = 4
    ACCEPT_PAYMENT = 5


def construct_rekey_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return REKEY_MOD


def curry_rekey_puzzle(old_prefarm_info: PrefarmInfo, new_prefarm_info: PrefarmInfo) -> Program:
    return construct_rekey_puzzle(old_prefarm_info).curry(
        build_merkle_tree(new_prefarm_info.puzzle_hash_list)[0],
        build_merkle_tree(old_prefarm_info.puzzle_hash_list)[0],
    )


def solve_rekey_completion(new_prefarm_info: PrefarmInfo):
    return Program.to([build_merkle_tree(new_prefarm_info.puzzle_hash_list)[0]])


def solve_rekey_clawback(puzzle_reveal: Program, solution: Program):
    return Program.to([puzzle_reveal, solution])


def construct_ach_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return ACH_MOD


def curry_ach_puzzle(prefarm_info: PrefarmInfo, p2_puzzle_hash: bytes32) -> Program:
    return construct_ach_puzzle(prefarm_info).curry(
        p2_puzzle_hash, construct_p2_singleton(prefarm_info.launcher_id).get_tree_hash()
    )


def solve_ach_completion():
    return Program.to([])


def solve_ach_clawback(puzzle_reveal: Program, solution: Program):
    return Program.to([puzzle_reveal, solution])


def construct_prefarm_inner_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return PREFARM_INNER.curry(
        PREFARM_INNER.get_tree_hash(),
        build_merkle_tree(prefarm_info.puzzle_hash_list)[0],
        [
            construct_rekey_puzzle(prefarm_info).get_tree_hash(),
            construct_ach_puzzle(prefarm_info).get_tree_hash(),
            construct_p2_singleton(prefarm_info.launcher_id).get_tree_hash(),
        ],
    )


def solve_prefarm_inner(spend_type: SpendType, prefarm_amount: uint64, **kwargs) -> Program:
    spend_solution: Program
    if spend_type == SpendType.FINISH_REKEY:
        spend_solution = Program.to(
            [
                build_merkle_tree(kwargs["puzzle_hash_list"])[0],
            ]
        )
    elif spend_type == SpendType.LOCK:
        spend_solution = Program.to(
            [
                kwargs["lock_puzzle"],
                kwargs["proof_of_inclusion"],
                build_merkle_tree(kwargs["puzzle_hash_list"])[0],
                kwargs["lock_puzzle_solution"],
            ]
        )
    elif spend_type == SpendType.START_REKEY:
        spend_solution = Program.to(
            [
                kwargs["puzzle_reveal"],
                kwargs["proof_of_inclusion"],
                build_merkle_tree(kwargs["puzzle_hash_list"])[0],
                kwargs["puzzle_solution"],
            ]
        )
    elif spend_type == SpendType.WITHDRAW_PAYMENT:
        spend_solution = Program.to(
            [
                kwargs["puzzle_reveal"],
                kwargs["proof_of_inclusion"],
                kwargs["withdrawal_amount"],
                kwargs["p2_ph"],
                kwargs["puzzle_solution"],
            ]
        )
    elif spend_type == SpendType.ACCEPT_PAYMENT:
        spend_solution = Program.to(kwargs["p2_singleton_lineage_proof"].to_program())
    else:
        raise ValueError("An invalid spend type was specified")

    return Program.to([prefarm_amount, spend_type.value, spend_solution])


def construct_singleton_inner_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return construct_rate_limiting_puzzle(
        prefarm_info.start_date,
        prefarm_info.starting_amount,
        prefarm_info.mojos_per_second,
        construct_prefarm_inner_puzzle(prefarm_info),
    )


def construct_full_singleton(
    launcher_id: bytes32,
    prefarm_info: PrefarmInfo,
) -> Program:
    return construct_singleton(
        launcher_id,
        construct_singleton_inner_puzzle(prefarm_info),
    )
