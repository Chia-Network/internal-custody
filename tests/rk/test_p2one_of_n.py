import clvm
import pytest

from chia.wallet.puzzles.p2_conditions import puzzle_for_conditions
from chia.types.blockchain_format.program import Program


from cic.rk.p2one_of_n import puzzle_for_puzzle_hashes, solution_for_inner_solution


def run_test_p2one_of_n(N):
    conditions = [b"%d" % _ for _ in range(N)]
    puzzles = [puzzle_for_conditions(_) for _ in conditions]
    puzzle_hashes = [_.get_tree_hash() for _ in puzzles]
    puzzle = puzzle_for_puzzle_hashes(puzzle_hashes)
    inner_solution = Program.to(0)
    for idx, inner_puzzle in enumerate(puzzles):
        solution = solution_for_inner_solution(puzzle_hashes, inner_puzzle, inner_solution)
        r = puzzle.run(solution)
        expected_output = Program.to(conditions[idx]).as_bin().hex()
        assert r.as_bin().hex() == expected_output

    with pytest.raises(clvm.EvalError.EvalError):
        bad_solution = Program.to([(1, 49), [1, 1500], 0])
        r = puzzle.run(bad_solution)


def test_n_is_2():
    run_test_p2one_of_n(2)
    run_test_p2one_of_n(3)
    run_test_p2one_of_n(4)
    run_test_p2one_of_n(8)
    run_test_p2one_of_n(16)
    run_test_p2one_of_n(32)
    run_test_p2one_of_n(320)
