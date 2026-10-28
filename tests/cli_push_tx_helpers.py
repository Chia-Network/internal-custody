from pathlib import Path
from types import SimpleNamespace
from typing import List, Tuple

from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import INFINITE_COST, Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.coin_spend import CoinSpend, make_spend

from chia._tests.util.spend_sim import SpendSim, SimClient

ACS: Program = Program.to(1)
ACS_PH: bytes32 = ACS.get_tree_hash()
LOCK: Program = Program.to([2, (1, 1), 1])
LOCK_PH: bytes32 = LOCK.get_tree_hash()


async def _setup_sim_coin() -> Coin:
    async with SpendSim.managed(Path("./sim.db")) as sim:
        await sim.farm_block(ACS_PH)
        sim_client = SimClient(sim)
        coin_records = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
        return sorted((record.coin for record in coin_records), key=lambda coin: coin.name())[0]


async def _setup_sim_coins(blocks: int = 2) -> List[Coin]:
    async with SpendSim.managed(Path("./sim.db")) as sim:
        for _ in range(blocks):
            await sim.farm_block(ACS_PH)
        sim_client = SimClient(sim)
        coin_records = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
        return sorted((record.coin for record in coin_records), key=lambda coin: coin.name())


async def _setup_single_wallet_coin() -> Coin:
    """Create one spendable ACS coin for the fee wallet and return a lock coin to push.

    The lock coin is farmed at a different puzzle hash, so the wallet only ever sees
    the single ACS coin.  Paying a fee spends that coin and creates a single change
    coin, which is exactly what the second fee has to use.
    """
    async with SpendSim.managed(Path("./sim.db")) as sim:
        await sim.farm_block(LOCK_PH)
        sim_client = SimClient(sim)
        coin_records = await sim_client.get_coin_records_by_puzzle_hashes([LOCK_PH], include_spent_coins=False)
        coins = sorted((record.coin for record in coin_records), key=lambda coin: coin.name())
        funding_coin, two_step_coin = coins[0], coins[1]
        spend = make_spend(funding_coin, LOCK, Program.to([[51, ACS_PH, funding_coin.amount]]))
        await sim_client.push_tx(SpendBundle([spend], G2Element()))
        await sim.farm_block(LOCK_PH)
        return two_step_coin


async def _open_sim_without_farming(db_path: str) -> object:
    from tests.cli_clients import _SimSession

    context_manager = SpendSim.managed(Path(db_path))
    sim = await context_manager.__aenter__()
    return _SimSession(sim, context_manager)


def _conditions_for_spend(coin_spend: CoinSpend) -> List[List[bytes]]:
    _, conditions = Program.from_bytes(bytes(coin_spend.puzzle_reveal)).run_with_cost(
        INFINITE_COST, Program.from_bytes(bytes(coin_spend.solution))
    )
    return conditions.as_python()


def _bundle_coin_ids(bundle: SpendBundle) -> set:
    return {coin_spend.coin.name() for coin_spend in bundle.coin_spends}


def _bundle_conditions(bundle: SpendBundle) -> List[List[bytes]]:
    return [condition for coin_spend in bundle.coin_spends for condition in _conditions_for_spend(coin_spend)]


def _fee_paid(bundle: SpendBundle) -> int:
    created_amount = sum(
        int.from_bytes(condition[2], "big")
        for condition in _bundle_conditions(bundle)
        if condition[0] == ConditionOpcode.CREATE_COIN.value
    )
    return sum(coin_spend.coin.amount for coin_spend in bundle.coin_spends) - created_amount


def _two_step_bundle_with_announcement(coin: Coin, puzzle: Program = ACS) -> Tuple[CoinSpend, CoinSpend, SpendBundle]:
    child_coin = Coin(coin.name(), coin.puzzle_hash, coin.amount)
    first_spend = make_spend(coin, puzzle, Program.to([[51, coin.puzzle_hash, coin.amount]]))
    second_spend = make_spend(
        child_coin,
        puzzle,
        Program.to([[80, 0], [60, b"msg"], [51, coin.puzzle_hash, child_coin.amount]]),
    )
    return first_spend, second_spend, SpendBundle([first_spend, second_spend], G2Element())


def _fee_bearing_bundle(coin: Coin, fee: int = 100) -> SpendBundle:
    return SpendBundle(
        [make_spend(coin, ACS, Program.to([[51, ACS_PH, coin.amount - fee]]))],
        G2Element(),
    )


def _announcement_bundle(coin: Coin) -> SpendBundle:
    return SpendBundle(
        [make_spend(coin, ACS, Program.to([[60, b"fee"], [51, ACS_PH, coin.amount]]))],
        G2Element(),
    )


def _record_wallet_calls(monkeypatch):
    from tests.cli_clients import FullNodeClientMock, WalletClientMock

    pushed_bundles: List[SpendBundle] = []
    fee_bundles: List[SpendBundle] = []
    fee_tx_configs: List = []
    original_push_tx = FullNodeClientMock.push_tx
    original_create_signed_transactions = WalletClientMock.create_signed_transactions

    async def record_push_tx(self, spend_bundle):
        pushed_bundles.append(spend_bundle)
        return await original_push_tx(self, spend_bundle)

    async def record_create_signed_transactions(self, request, tx_config, extra_conditions=()):
        tx_response = await original_create_signed_transactions(self, request, tx_config, extra_conditions)
        fee_bundles.append(tx_response.signed_txs[0].spend_bundle)
        fee_tx_configs.append(tx_config)
        return tx_response

    monkeypatch.setattr(FullNodeClientMock, "push_tx", record_push_tx)
    monkeypatch.setattr(WalletClientMock, "create_signed_transactions", record_create_signed_transactions)
    return pushed_bundles, fee_bundles, fee_tx_configs


def _select_spent_coin_on_second_call(monkeypatch, spent_coin: Coin):
    from tests.cli_clients import WalletClientMock

    original_select_coins = WalletClientMock._select_coins
    calls: List[object] = []

    async def select_spent_coin(self, request, tx_config=None):
        calls.append(tx_config)
        if len(calls) == 2:
            return SimpleNamespace(coins=[spent_coin])
        return await original_select_coins(self, request, tx_config)

    monkeypatch.setattr(WalletClientMock, "_select_coins", select_spent_coin)
    return calls
