from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32

from cic.rk.load_clvm import load_clvm

MOD = load_clvm("p2a_or_b.clsp", package_or_requirement=__package__, include_paths=["../clsp/include/"])


def puzzle_for_puzzle_hashes(puzzle_hash_a: bytes32, puzzle_hash_b: bytes32) -> Program:
    return MOD.curry(puzzle_hash_a, puzzle_hash_b)


def solution_for_inner_solution(puzzle_reveal: Program, inner_solution: Program) -> Program:
    solution = Program.to([puzzle_reveal, inner_solution])
    return solution
