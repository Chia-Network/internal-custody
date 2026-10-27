from chia_rs import Coin, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64

from chia._tests.util.spend_sim import SimBlockRecord, SimFullBlock, SpendSim
from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.consensus.coinbase import create_farmer_coin, create_pool_coin
from chia.types.blockchain_format.program import INFINITE_COST, Program
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.util.errors import Err


def run_coin_spend(coin_spend):
    puzzle_reveal = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
    solution = Program.from_bytes(bytes(coin_spend.solution))
    return puzzle_reveal.run_with_cost(INFINITE_COST, solution)


async def farm_block_with_bundle(sim: SpendSim, spend_bundle: SpendBundle) -> None:
    tx_additions = []
    tx_removals = []
    fee = 0
    for coin_spend in spend_bundle.coin_spends:
        _, conditions = run_coin_spend(coin_spend)
        fee += int(coin_spend.coin.amount)
        tx_removals.append(coin_spend.coin.name())
        for condition in conditions.as_python():
            if int.from_bytes(condition[0], "big") == 51:
                child = Coin(
                    coin_spend.coin.name(),
                    bytes32(condition[1]),
                    uint64(int.from_bytes(condition[2], "big")),
                )
                fee -= int(child.amount)
                same_as_parent = (
                    child.puzzle_hash == coin_spend.coin.puzzle_hash and child.amount == coin_spend.coin.amount
                )
                tx_additions.append((child.name(), child, same_as_parent))

    reward_puzzle_hash = bytes32(b"0" * 32)
    next_block_height = uint32(sim.block_height + 1) if len(sim.block_records) > 0 else sim.block_height
    included_reward_coins = [
        create_pool_coin(
            next_block_height,
            reward_puzzle_hash,
            calculate_pool_reward(next_block_height),
            sim.defaults.GENESIS_CHALLENGE,
        ),
        create_farmer_coin(
            next_block_height,
            reward_puzzle_hash,
            uint64(calculate_base_farmer_reward(next_block_height) + fee),
            sim.defaults.GENESIS_CHALLENGE,
        ),
    ]

    await sim.coin_store.new_block(
        height=uint32(sim.block_height + 1),
        timestamp=sim.timestamp,
        included_reward_coins=included_reward_coins,
        tx_additions=tx_additions,
        tx_removals=tx_removals,
    )
    generator = await sim.generate_transaction_generator(spend_bundle)
    sim.block_records.append(SimBlockRecord.create(included_reward_coins, next_block_height, sim.timestamp))
    sim.blocks.append(SimFullBlock(generator, next_block_height))
    sim.block_height = next_block_height
    await sim.new_peak(tx_removals)


async def push_tx_with_fallback(sim: SpendSim, sim_client, spend_bundle: SpendBundle):
    result = await sim_client.push_tx(spend_bundle)
    if result == (MempoolInclusionStatus.FAILED, Err.EPHEMERAL_RELATIVE_CONDITION):
        await farm_block_with_bundle(sim, spend_bundle)
        return MempoolInclusionStatus.SUCCESS, None
    return result
