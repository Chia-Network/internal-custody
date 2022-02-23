from chia.wallet.puzzles.p2_conditions import puzzle_for_conditions
from chia.types.blockchain_format.program import Program

from cic.rk.p2ach import puzzle_for_ach, solution_for_clawback, solution_for_claim_redemption


def test_p2ach():
    clawback_puzzle = puzzle_for_conditions([[b"CLAWBACK"]])
    clawback_puzzle_hash = clawback_puzzle.get_tree_hash()

    claim_puzzle = puzzle_for_conditions([[b"REDEMPTION"]])
    claim_puzzle_hash = claim_puzzle.get_tree_hash()

    delay = 19991
    puzzle = puzzle_for_ach(clawback_puzzle_hash, claim_puzzle_hash, delay)

    blank_solution = Program.to(b"")

    solution_clawback = solution_for_clawback(clawback_puzzle, claim_puzzle_hash, delay, blank_solution)
    r = puzzle.run(solution_clawback)
    assert r.as_bin().hex() == "ffff88434c41574241434b8080"

    solution_redemption = solution_for_claim_redemption(clawback_puzzle_hash, claim_puzzle, delay, blank_solution)
    r = puzzle.run(solution_redemption)
    assert r.as_bin().hex() == "ffff50ff824e1780ffff8a524544454d5054494f4e8080"
