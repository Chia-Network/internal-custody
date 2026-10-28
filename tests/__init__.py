from typing import Any, Tuple

from chia_rs import SpendBundle

from chia._tests.util.spend_sim import SpendSim
from chia.types.blockchain_format.program import INFINITE_COST, Program
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.util.errors import Err

from cic.drivers.two_step_push import split_two_step_push


def run_coin_spend(coin_spend):
    puzzle_reveal = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
    solution = Program.from_bytes(bytes(coin_spend.solution))
    return puzzle_reveal.run_with_cost(INFINITE_COST, solution)


async def push_two_step_bundle(sim: SpendSim, sim_client: Any, bundle: SpendBundle) -> Tuple[Any, Any]:
    result = await sim_client.push_tx(bundle)
    assert result == (MempoolInclusionStatus.FAILED, Err.EPHEMERAL_RELATIVE_CONDITION)

    two_step = split_two_step_push(bundle)

    first_result = await sim_client.push_tx(two_step.first)
    assert first_result == (MempoolInclusionStatus.SUCCESS, None)

    second_result_before_block = await sim_client.push_tx(two_step.second)
    assert second_result_before_block == (MempoolInclusionStatus.FAILED, Err.UNKNOWN_UNSPENT)

    await sim.farm_block()
    for coin_spend in two_step.second.coin_spends:
        record = await sim_client.get_coin_record_by_name(coin_spend.coin.name())
        assert record is not None
        assert record.confirmed_block_index > 0
        assert not record.spent

    second_result = await sim_client.push_tx(two_step.second)
    assert second_result == (MempoolInclusionStatus.SUCCESS, None)

    await sim.farm_block()
    return first_result, second_result
