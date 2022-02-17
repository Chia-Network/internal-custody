import clvm
import pytest

from chia.wallet.puzzles.p2_conditions import puzzle_for_conditions
from chia.types.blockchain_format.program import Program


from cic.rk.p2a_or_b import puzzle_for_puzzle_hashes, solution_for_inner_solution


def test_p2_a_or_b():
    puzzle_a = puzzle_for_conditions([b"FOO"])
    puzzle_b = puzzle_for_conditions([b"BAR"])
    puzzle = puzzle_for_puzzle_hashes(puzzle_a.get_tree_hash(), puzzle_b.get_tree_hash())
    solution_for_a = solution_for_inner_solution(puzzle_a, Program.to(b""))
    r = puzzle.run(solution_for_a)
    assert r.as_bin().hex() == "ff83464f4f80"

    solution_for_b = solution_for_inner_solution(puzzle_b, Program.to(b""))
    r = puzzle.run(solution_for_b)
    assert r.as_bin().hex() == "ff8342415280"

    with pytest.raises(clvm.EvalError.EvalError):
        bad_solution = Program.to((1, 500))
        puzzle.run(bad_solution)
