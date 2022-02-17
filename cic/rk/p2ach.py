from cic.p2a_or_b import puzzle_for_puzzle_hashes, solution_for_inner_solution
from cic.p2delay import morph_puzzle_hash_with_delay, morph_puzzle_with_delay

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32


def puzzle_for_ach(clawback_puzzle_hash: bytes32, claim_puzzle_hash: bytes32, delay_in_seconds: int) -> Program:
    wrapped_claim_puzzle_hash = morph_puzzle_hash_with_delay(claim_puzzle_hash, delay_in_seconds)
    puzzle = puzzle_for_puzzle_hashes(clawback_puzzle_hash, wrapped_claim_puzzle_hash)
    return puzzle


def solution_for_clawback(
    clawback_puzzle: Program, claim_puzzle_hash: bytes32, delay_in_seconds: int, clawback_solution: Program
) -> Program:
    solution = solution_for_inner_solution(clawback_puzzle, clawback_solution)
    return solution


def solution_for_claim_redemption(
    clawback_puzzle_hash: bytes32, claim_puzzle: Program, delay_in_seconds: int, claim_solution: Program
) -> Program:
    wrapped_claim_puzzle = morph_puzzle_with_delay(claim_puzzle, delay_in_seconds)
    solution = solution_for_inner_solution(wrapped_claim_puzzle, claim_solution)
    return solution
