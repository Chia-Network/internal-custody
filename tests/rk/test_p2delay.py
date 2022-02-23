from chia.types.blockchain_format.program import Program

from cic.rk.p2delay import morph_puzzle_hash_with_delay, morph_puzzle_with_delay


def test_p2delay():
    anyone_can_spend_puzzle = Program.to(1)
    anyone_can_spend_solution = Program.to([[b"DELAY", 1], [b"DELAY", 2]])

    r = anyone_can_spend_puzzle.run(anyone_can_spend_solution)
    assert r.as_bin().hex() == "ffff8544454c4159ff0180ffff8544454c4159ff028080"

    delay = 0xDEADBEEF
    delayed_puzzle = morph_puzzle_with_delay(anyone_can_spend_puzzle, delay)
    assert delayed_puzzle.get_tree_hash() == morph_puzzle_hash_with_delay(
        anyone_can_spend_puzzle.get_tree_hash(), delay
    )

    r = delayed_puzzle.run(anyone_can_spend_solution)
    assert r.as_bin().hex() == "ffff50ff8500deadbeef80ffff8544454c4159ff0180ffff8544454c4159ff028080"
