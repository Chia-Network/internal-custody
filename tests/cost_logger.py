import json

from chia.types.blockchain_format.program import INFINITE_COST
from chia.types.spend_bundle import SpendBundle
from chia.types.generator_types import BlockGenerator
from chia.consensus.cost_calculator import NPCResult
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.full_node.bundle_tools import simple_solution_generator
from chia.full_node.mempool_check_conditions import get_name_puzzle_conditions


class CostLogger:
    def __init__(self):
        self.cost_dict = {}
        self.cost_dict_no_puzs = {}

    def add_cost(self, descriptor: str, spend_bundle: SpendBundle):
        program: BlockGenerator = simple_solution_generator(spend_bundle)
        npc_result: NPCResult = get_name_puzzle_conditions(
            program, INFINITE_COST, cost_per_byte=DEFAULT_CONSTANTS.COST_PER_BYTE, mempool_mode=True
        )
        self.cost_dict[descriptor] = npc_result.cost
        cost_to_subtract: int = 0
        for cs in spend_bundle.coin_spends:
            cost_to_subtract += len(bytes(cs.puzzle_reveal)) * DEFAULT_CONSTANTS.COST_PER_BYTE
        self.cost_dict_no_puzs[descriptor] = npc_result.cost - cost_to_subtract

    def log_cost_statistics(self):
        merged_dict = {
            "standard cost": self.cost_dict,
            "no puzzle reveals": self.cost_dict_no_puzs,
        }
        print(json.dumps(merged_dict, indent=4))
