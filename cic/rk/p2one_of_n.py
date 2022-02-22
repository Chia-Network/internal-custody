from typing import List

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32

from cic.rk.load_clvm import load_clvm
from cic.rk.merkle_util import list_to_binary_tree, build_merkle_tree_from_binary_tree, TupleTree

MOD = load_clvm(
    "p2_inner_puzzle_merkle_parameters.clsp", package_or_requirement=__package__, include_paths=["../clsp/include/"]
)


INNER_PUZZLE = Program.from_bytes(bytes.fromhex("ff02ff02ff0580"))  # `(a 2 5)`


def puzzle_for_puzzle_hash_tree(puzzle_hash_binary_tree: TupleTree) -> Program:
    merkle_root, proofs = build_merkle_tree_from_binary_tree(puzzle_hash_binary_tree)
    return MOD.curry(INNER_PUZZLE, merkle_root)


def solution_for_inner_solution_with_puzzle_tree(
    puzzle_hash_binary_tree: TupleTree, puzzle_reveal: Program, inner_solution: Program
) -> Program:
    merkle_root, proofs = build_merkle_tree_from_binary_tree(puzzle_hash_binary_tree)
    tree_hash = puzzle_reveal.get_tree_hash()
    proof = proofs.get(tree_hash)
    solution = Program.to([puzzle_reveal, proof, inner_solution])
    return solution


def puzzle_for_puzzle_hashes(puzzle_hash_list: List[bytes32]) -> Program:
    binary_tree = list_to_binary_tree(puzzle_hash_list)
    return puzzle_for_puzzle_hash_tree(binary_tree)


def solution_for_inner_solution(
    puzzle_hash_list: List[bytes32], puzzle_reveal: Program, inner_solution: Program
) -> Program:
    binary_tree = list_to_binary_tree(puzzle_hash_list)
    return solution_for_inner_solution_with_puzzle_tree(binary_tree, puzzle_reveal, inner_solution)
