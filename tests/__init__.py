from chia.types.blockchain_format.program import INFINITE_COST, Program


def run_coin_spend(coin_spend):
    puzzle_reveal = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
    solution = Program.from_bytes(bytes(coin_spend.solution))
    return puzzle_reveal.run_with_cost(INFINITE_COST, solution)
