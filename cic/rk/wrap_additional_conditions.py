from typing import List

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32

from .hashutils import (
    sha256pair,
    A_BLOB,
    C_BLOB,
    Q_BLOB,
    A_TREEHASH,
    C_TREEHASH,
    Q_TREEHASH,
    ONE_TREEHASH,
    NULL_TREEHASH,
)


def morph_puzzle_with_conditions(puzzle: Program, conditions: List[Program]) -> Program:
    # puzzle => (a (q . puzzle) 1)
    # c1 => `(c c1 (a (q . puzzle) 1))`
    # c2 => `(c c2 (c c1 (a (q . puzzle) 1)))`
    # etc.
    final_puzzle = puzzle.to([A_BLOB, (Q_BLOB, puzzle), 1])
    for condition in reversed(conditions):
        final_puzzle = Program.to([C_BLOB, (Q_BLOB, condition), final_puzzle])
    return final_puzzle


def morph_puzzle_hash_with_conditions(puzzle_hash: bytes32, conditions: List[Program]) -> bytes32:
    final_puzzle_hash = sha256pair(
        A_TREEHASH, sha256pair(sha256pair(Q_TREEHASH, puzzle_hash), sha256pair(ONE_TREEHASH, NULL_TREEHASH))
    )
    for condition in reversed(conditions):
        final_puzzle_hash = sha256pair(
            C_TREEHASH,
            sha256pair(sha256pair(Q_TREEHASH, condition.get_tree_hash()), sha256pair(final_puzzle_hash, NULL_TREEHASH)),
        )
    return final_puzzle_hash
