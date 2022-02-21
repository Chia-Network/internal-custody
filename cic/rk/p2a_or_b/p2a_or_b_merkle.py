from ..merkle_util import build_merkle_tree

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32

from cic.rk.load_clvm import load_clvm
from cic.rk.merkle_util import list_to_2_tuples, build_merkle_tree_from_2_tuples

MOD = load_clvm("p2_inner_puzzle_merkle_parameters.clsp", package_or_requirement=__package__, include_paths=["../clsp/include/"])


CODE = "(mod (parameters solution) (a parameters solution))"
INNER_PUZZLE = Program.from_bytes(bytes.fromhex("ff02ff02ff0580"))


def puzzle_for_puzzle_hashes(puzzle_hash_a: bytes32, puzzle_hash_b: bytes32) -> Program:
    merkle_root, proofs = build_merkle_tree_from_2_tuples((puzzle_hash_a, puzzle_hash_b))
    return MOD.curry(INNER_PUZZLE, merkle_root)


def solution_for_inner_solution(puzzle_hash_a: bytes32, puzzle_hash_b: bytes32, puzzle_reveal: Program, inner_solution: Program) -> Program:
    if puzzle_reveal.get_tree_hash() == puzzle_hash_a:
        solution = Program.to([puzzle_reveal, (0, [puzzle_hash_b]), inner_solution])
    else:
        solution = Program.to([puzzle_reveal, (1, [puzzle_hash_a]), inner_solution])
    return solution
