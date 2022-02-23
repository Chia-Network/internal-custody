from chia.types.blockchain_format.program import Program


from cic.rk.wrap_additional_conditions import morph_puzzle_with_conditions, morph_puzzle_hash_with_conditions


def run_test_additional_conditions(C):
    C = [Program.to(_) for _ in C]
    inner_puzzle = Program.to(1)
    morphed_puzzle = morph_puzzle_with_conditions(inner_puzzle, C)

    mp1 = morphed_puzzle.get_tree_hash()
    iph = inner_puzzle.get_tree_hash()
    assert morph_puzzle_hash_with_conditions(iph, C) == mp1

    blank_solution = Program.to(0)
    r = morphed_puzzle.run(blank_solution)

    assert r.as_bin().hex() == Program.to(C).as_bin().hex()


def test_additional_conditions():
    run_test_additional_conditions([])
    run_test_additional_conditions([88])
    run_test_additional_conditions([[100, 200, 300], [400, 500, 600]])
