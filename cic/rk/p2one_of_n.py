from typing import List

from .merkle_util import build_merkle_tree

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32

from cic.rk.load_clvm import load_clvm
from cic.rk.merkle_util import list_to_binary_tree, build_merkle_tree_from_binary_tree

MOD = load_clvm(
    "p2_inner_puzzle_merkle_parameters.clsp", package_or_requirement=__package__, include_paths=["../clsp/include/"]
)


INNER_PUZZLE = Program.from_bytes(bytes.fromhex("ff02ff02ff0580"))  # `(a 2 5)`


def puzzle_for_puzzle_hashes(puzzle_hash_list: List[bytes32]) -> Program:
    merkle_root, proofs = build_merkle_tree_from_binary_tree(puzzle_hash_list)
    return MOD.curry(INNER_PUZZLE, merkle_root)


def solution_for_inner_solution(
    puzzle_hash_list: List[bytes32], puzzle_reveal: Program, inner_solution: Program
) -> Program:
    merkle_root, proofs = build_merkle_tree_from_binary_tree(puzzle_hash_list)
    tree_hash = puzzle_reveal.get_tree_hash()
    proof = proofs.get(tree_hash)
    solution = Program.to([puzzle_reveal, proof, inner_solution])
    return solution
