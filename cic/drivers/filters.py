from typing import List, Tuple, Dict

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64
from chia.wallet.lineage_proof import LineageProof

from cic.drivers.drop_coins import (
    P2_MERKLE_MOD,
    construct_rekey_puzzle,
    construct_ach_puzzle,
    calculate_ach_clawback_ph,
)
from cic.drivers.merkle_utils import build_merkle_tree
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.singleton import SINGLETON_MOD, SINGLETON_LAUNCHER_HASH, construct_p2_singleton
from cic.load_clvm import load_clvm

FILTER_ONLY_REKEY_MOD = load_clvm("only_rekey.clsp", package_or_requirement="cic.clsp.filters")
FILTER_REKEY_AND_PAYMENT_MOD = load_clvm("rekey_and_payment.clsp", package_or_requirement="cic.clsp.filters")

def construct_payment_and_rekey_filter(
    prefarm_info: PrefarmInfo,
    puzzle_hash_list: List[bytes32],
    rekey_timelock: uint64,
) -> Program:
    return P2_MERKLE_MOD.curry(
        FILTER_REKEY_AND_PAYMENT_MOD.curry(
            [
                construct_rekey_puzzle(prefarm_info).get_tree_hash(),
                rekey_timelock,
                construct_ach_puzzle(prefarm_info).get_tree_hash(),
                calculate_ach_clawback_ph(prefarm_info),
            ],
        ),
        build_merkle_tree(puzzle_hash_list)[0],
        [],
    )


def construct_rekey_filter(
    prefarm_info: PrefarmInfo,
    puzzle_hash_list: List[bytes32],
    rekey_timelock: uint64,
) -> Program:
    return P2_MERKLE_MOD.curry(
        FILTER_ONLY_REKEY_MOD.curry(
            [
                construct_rekey_puzzle(prefarm_info).get_tree_hash(),
                rekey_timelock,
            ],
        ),
        build_merkle_tree(puzzle_hash_list)[0],
        [],
    )


def solve_filter_for_payment(
    puzzle_reveal: Program,
    proof_of_inclusion: Program,
    puzzle_solution: Program,
    puzzle_hash_list: List[bytes32],
    p2_ph: bytes32,
):
    return Program.to(
        [
            puzzle_reveal,
            proof_of_inclusion,
            [
                puzzle_solution,
                (build_merkle_tree(puzzle_hash_list)[0], p2_ph),
            ],
        ]
    )


def solve_filter_for_rekey(
    puzzle_reveal: Program,
    proof_of_inclusion: Program,
    puzzle_solution: Program,
    old_puzzle_hash_list: List[bytes32],
    new_puzzle_hash_list: List[bytes32],
    timelock: uint64,
):
    return Program.to(
        [
            puzzle_reveal,
            proof_of_inclusion,
            [
                puzzle_solution,
                [build_merkle_tree(new_puzzle_hash_list)[0], build_merkle_tree(old_puzzle_hash_list)[0], timelock],
            ],
        ]
    )