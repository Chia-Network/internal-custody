import enum

from dataclasses import dataclass
from typing import List, Tuple, Dict

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.merkle_utils import build_merkle_tree
from cic.drivers.singleton import SINGLETON_MOD, SINGLETON_LAUNCHER_HASH, construct_singleton, construct_p2_singleton
from cic.drivers.rate_limiting import construct_rate_limiting_puzzle
from cic.load_clvm import load_clvm


PREFARM_INNER = load_clvm("prefarm_inner.clsp", package_or_requirement="cic.clsp.singleton")
P2_MERKLE_MOD = load_clvm("p2_merkle_tree.clsp", package_or_requirement="cic.clsp.drop_coins")
REKEY_COMPLETION_MOD = load_clvm("rekey_completion.clsp", package_or_requirement="cic.clsp.drop_coins")
REKEY_CLAWBACK_MOD = load_clvm("rekey_clawback.clsp", package_or_requirement="cic.clsp.drop_coins")
ACH_COMPLETION_MOD = load_clvm("ach_completion.clsp", package_or_requirement="cic.clsp.drop_coins")
ACH_CLAWBACK_MOD = load_clvm("ach_clawback.clsp", package_or_requirement="cic.clsp.drop_coins")


@dataclass
class PrefarmInfo:
    launcher_id: bytes32
    start_date: uint64
    starting_amount: uint64
    mojos_per_second: uint64
    puzzle_hash_list: List[bytes32]
    clawback_period: uint64


class SpendType(int, enum.Enum):
    FINISH_REKEY = 1
    START_REKEY = 2
    HANDLE_PAYMENT = 3


def construct_rekey_completion(launcher_id: bytes32) -> Program:
    return REKEY_COMPLETION_MOD.curry((SINGLETON_MOD.get_tree_hash(), (launcher_id, SINGLETON_LAUNCHER_HASH)))


def construct_rekey_clawback() -> Program:
    return REKEY_CLAWBACK_MOD


def calculate_rekey_merkle_tree(launcher_id: bytes32) -> Tuple[bytes32, Dict[bytes32, Tuple[int, List[bytes32]]]]:
    return build_merkle_tree(
        [
            construct_rekey_completion(launcher_id).get_tree_hash(),
            construct_rekey_clawback().get_tree_hash(),
        ]
    )


def construct_rekey_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return P2_MERKLE_MOD.curry(calculate_rekey_merkle_tree(prefarm_info.launcher_id)[0])


def curry_rekey_puzzle(timelock: uint64, old_prefarm_info: PrefarmInfo, new_prefarm_info: PrefarmInfo) -> Program:
    return construct_rekey_puzzle(old_prefarm_info).curry(
        [
            build_merkle_tree(new_prefarm_info.puzzle_hash_list)[0],
            build_merkle_tree(old_prefarm_info.puzzle_hash_list)[0],
            timelock,
        ]
    )


def solve_rekey_completion(launcher_id: bytes32, lineage_proof: LineageProof):
    completion_puzzle: Program = construct_rekey_completion(launcher_id)
    merkle_proofs: Dict[bytes32, Tuple[int, List[bytes32]]] = calculate_rekey_merkle_tree(launcher_id)[1]
    completion_proof = Program.to(merkle_proofs[completion_puzzle.get_tree_hash()])

    return Program.to(
        [
            completion_puzzle,
            completion_proof,
            [
                lineage_proof.to_program(),
            ],
        ]
    )


def solve_rekey_clawback(launcher_id: bytes32, puzzle_reveal: Program, proof_of_inclusion: Program, solution: Program):
    clawback_puzzle: Program = construct_rekey_clawback()
    merkle_proofs: Dict[bytes32, Tuple[int, List[bytes32]]] = calculate_rekey_merkle_tree(launcher_id)[1]
    clawback_proof = Program.to(merkle_proofs[clawback_puzzle.get_tree_hash()])

    return Program.to(
        [
            clawback_puzzle,
            clawback_proof,
            [
                puzzle_reveal,
                proof_of_inclusion,
                solution,
            ],
        ]
    )


def construct_ach_completion(timelock: uint64) -> Program:
    return ACH_COMPLETION_MOD.curry(timelock)


def construct_ach_clawback(launcher_id: bytes32) -> Program:
    return ACH_CLAWBACK_MOD.curry(construct_p2_singleton(launcher_id).get_tree_hash())


def calculate_ach_merkle_tree(
    launcher_id: bytes32, timelock: uint64
) -> Tuple[bytes32, Dict[bytes32, Tuple[int, List[bytes32]]]]:
    return build_merkle_tree(
        [
            construct_ach_completion(timelock).get_tree_hash(),
            construct_ach_clawback(launcher_id).get_tree_hash(),
        ]
    )


def construct_ach_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return P2_MERKLE_MOD.curry(calculate_ach_merkle_tree(prefarm_info.launcher_id, prefarm_info.clawback_period)[0])


def curry_ach_puzzle(prefarm_info: PrefarmInfo, p2_puzzle_hash: bytes32) -> Program:
    return construct_ach_puzzle(prefarm_info).curry(
        (
            build_merkle_tree(prefarm_info.puzzle_hash_list)[0],
            p2_puzzle_hash,
        )
    )


def solve_ach_completion(amount: uint64):
    return Program.to([amount])


def solve_ach_clawback(amount: uint64, puzzle_reveal: Program, proof_of_inclusion: Program, solution: Program):
    return Program.to([amount, puzzle_reveal, proof_of_inclusion, solution])


def construct_prefarm_inner_puzzle(prefarm_info: PrefarmInfo) -> Program:
    return PREFARM_INNER.curry(
        PREFARM_INNER.get_tree_hash(),
        build_merkle_tree(prefarm_info.puzzle_hash_list)[0],
        [
            construct_rekey_puzzle(prefarm_info).get_tree_hash(),
            construct_ach_puzzle(prefarm_info).get_tree_hash(),
        ],
    )


def solve_prefarm_inner(spend_type: SpendType, prefarm_amount: uint64, **kwargs) -> Program:
    spend_solution: Program
    if spend_type in [SpendType.START_REKEY, SpendType.FINISH_REKEY]:
        spend_solution = Program.to(
            [
                kwargs["timelock"],
                build_merkle_tree(kwargs["puzzle_hash_list"])[0],
            ]
        )
    elif spend_type == SpendType.HANDLE_PAYMENT:
        spend_solution = Program.to(
            [
                kwargs["payment_amount"],
                kwargs["p2_ph"],
            ]
        )
    else:
        raise ValueError("An invalid spend type was specified")

    return Program.to(
        [
            prefarm_amount,
            spend_type.value,
            spend_solution,
            kwargs.get("puzzle_reveal"),
            kwargs.get("proof_of_inclusion"),
            kwargs.get("puzzle_solution"),
        ]
    )


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
