from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32

from cic.rk.wrap_additional_conditions import morph_puzzle_with_conditions, morph_puzzle_hash_with_conditions


ASSERT_SECONDS_RELATIVE_BLOB = 80


def morph_puzzle_hash_with_delay(puzzle_hash: bytes32, delay_in_seconds: int) -> bytes32:
    conditions = [Program.to([ASSERT_SECONDS_RELATIVE_BLOB, delay_in_seconds])]
    return morph_puzzle_hash_with_conditions(puzzle_hash, conditions)


def morph_puzzle_with_delay(puzzle: Program, delay_in_seconds: int) -> Program:
    conditions = [Program.to([ASSERT_SECONDS_RELATIVE_BLOB, delay_in_seconds])]
    return morph_puzzle_with_conditions(puzzle, conditions)
