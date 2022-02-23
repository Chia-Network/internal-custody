from typing import Tuple

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32


from .hashutils import (
    sha256pair,
    A_TREEHASH,
    C_TREEHASH,
    Q_TREEHASH,
    ONE_TREEHASH,
    NULL_TREEHASH,
)


def anonymous_curry(template_hash: bytes32, *parameter_hashes: Tuple[bytes32]) -> bytes32:
    """
    This curious function lets you calculate the puzzle hash that would result when
    currying an existing template with certain parameters, WITHOUT KNOWING either the
    template or the parameters -- just their hashes.

    It can be used to wrap someone else's puzzle hash without knowing the underlying puzzle.

    Note that `curry(P, p1, p2, p3)` => "(a (q . "P") (c (q . "p1") (c (q . "p2") (c (q . "p3") 1))))".

    We do this in two steps: first, we start with the `1`, then recursively build up the
    `(c (q . PARAMETER) PREVIOUS_CONTENT)` currying bindings, calculating tree hashes along the way.

    Once all the parameters have been added, we then prepend `(a (q . TEMPLATE) PREVIOUS_CONTENT)`.
    """

    current_hash = ONE_TREEHASH
    for parameter_hash in reversed(parameter_hashes):
        current_hash_in_list = sha256pair(current_hash, NULL_TREEHASH)
        quoted_parameter_hash = sha256pair(Q_TREEHASH, parameter_hash)
        extended_list_hash = sha256pair(quoted_parameter_hash, current_hash_in_list)
        current_hash = sha256pair(C_TREEHASH, extended_list_hash)

    # we now have the hash of `(c (q . "p1") (c (q . "p2") (c (q . "p3") 1)))`
    parameters_in_list_hash = sha256pair(current_hash, NULL_TREEHASH)
    quoted_template_hash = sha256pair(Q_TREEHASH, template_hash)
    extended_list_hash = sha256pair(quoted_template_hash, parameters_in_list_hash)
    final_hash = sha256pair(A_TREEHASH, extended_list_hash)
    return final_hash


def test_anonymous_curry(puzzle: Program, *args: Program):
    p1_curried = puzzle.curry(*list(args))
    p1 = p1_curried.get_tree_hash()
    p2 = anonymous_curry(puzzle.get_tree_hash(), *[Program.to(_).get_tree_hash() for _ in args])
    print(p1)
    print(p2)
    assert p1 == p2


test_anonymous_curry(Program.to(0))
test_anonymous_curry(Program.to(5000))
test_anonymous_curry(Program.to([5000, 30, 192373]))
test_anonymous_curry(Program.to(0), Program.to(0))
test_anonymous_curry(Program.to(0), Program.to(1))
test_anonymous_curry(Program.to(0), 1, 2, 3)
