from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32

from cic.drivers.load_clvm import load_clvm

from .anonymous_curry import anonymous_curry


MOD = load_clvm("p2delay.clsp", package_or_requirement=__package__, include_paths=["./clsp/include/"])


def morph_puzzle_hash_with_delay(puzzle_hash: bytes32, delay_in_seconds: int) -> bytes32:
    delay_hash = Program.to(delay_in_seconds).get_tree_hash()
    return anonymous_curry(MOD.get_tree_hash(), puzzle_hash, delay_hash)


def morph_puzzle_with_delay(puzzle: Program, delay_in_seconds: int) -> Program:
    return MOD.curry(puzzle, delay_in_seconds)
