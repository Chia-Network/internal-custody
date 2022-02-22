from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32

from cic.rk.p2a_or_b import p2one_of_n


def puzzle_for_puzzle_hashes(puzzle_hash_a: bytes32, puzzle_hash_b: bytes32) -> Program:
    return p2one_of_n.puzzle_for_puzzle_hashes([puzzle_hash_a, puzzle_hash_b])


def solution_for_inner_solution(
    puzzle_hash_a: bytes32, puzzle_hash_b: bytes32, puzzle_reveal: Program, inner_solution: Program
) -> Program:
    return p2one_of_n.solution_for_inner_solution([puzzle_hash_a, puzzle_hash_b], puzzle_reveal, inner_solution)
