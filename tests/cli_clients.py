from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from chia_rs import G2Element, SpendBundle
from chia_rs.sized_ints import uint32, uint64

from chia._tests.util.spend_sim import SpendSim, SimClient
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend, make_spend
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.wallet.wallet_request_types import CreateSignedTransaction, SelectCoins

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


class _SimSession:
    def __init__(self, sim: SpendSim, context_manager: Any):
        self.sim = sim
        self._context_manager = context_manager

    async def close(self) -> None:
        if self._context_manager is not None:
            context_manager, self._context_manager = self._context_manager, None
            await context_manager.__aexit__(None, None, None)


async def _open_sim(db_path: str) -> _SimSession:
    context_manager = SpendSim.managed(Path(db_path))
    sim = await context_manager.__aenter__()
    await sim.farm_block(ACS_PH)
    return _SimSession(sim, context_manager)


class FullNodeClientMock(SimClient):
    # We're just overriding the push_tx endpoint to return a dict and farm a block when done
    def __init__(self, session: _SimSession):
        super().__init__(session.sim)
        self.session = session

    async def push_tx(self, spend_bundle: SpendBundle) -> Dict[str, Any]:  # type: ignore
        result = await super().push_tx(spend_bundle)
        if result[0] == MempoolInclusionStatus.SUCCESS:
            await self.service.farm_block()
            return {"success": True, "status": MempoolInclusionStatus.SUCCESS.name}
        else:
            return {"success": False, "error": result[1]}

    async def get_puzzle_and_solution(self, coin_id, height):
        if height == 0:
            return None
        return await super().get_puzzle_and_solution(coin_id, height)

    def close(self):
        return

    async def await_closed(self):
        await self.session.close()


class TXMock:
    def __init__(self, bundle):
        self.spend_bundle = bundle


class WalletClientMock:
    def __init__(self, sim_client):
        self.sim_client = sim_client

    # These are the only two methods we need
    async def select_coins(self, request: SelectCoins):
        return SimpleNamespace(
            coins=[
                sorted(
                    await self.sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False),
                    key=lambda cr: cr.coin.name(),
                )[0].coin
            ]
        )

    async def create_signed_transactions(
        self,
        request: CreateSignedTransaction,
        tx_config: Any,
        extra_conditions: tuple = (),
    ):
        assert request.push is False and request.sign is True
        total_amount: int = 0
        conditions: List[Program] = []
        for add in request.additions:
            total_amount += add.amount
            conditions.append(Program.to([51, add.puzzle_hash, add.amount]))
        for condition in extra_conditions:
            conditions.append(condition.to_program())
        coins: Optional[List[Coin]] = request.coins
        if coins is None:
            coins = (
                await self.select_coins(
                    SelectCoins(wallet_id=uint32(1), amount=uint64(total_amount + request.fee)),
                )
            ).coins
        change_amount = sum(c.amount for c in coins) - (total_amount + request.fee)
        conditions.append(Program.to([51, ACS_PH, change_amount]))  # change
        coin_spends: List[CoinSpend] = [make_spend(coins[0], ACS, Program.to(conditions))]
        if len(coins) > 1:
            for coin in coins[1:]:
                coin_spends.append(make_spend(coin, ACS, Program.to([])))
        return SimpleNamespace(signed_txs=[TXMock(SpendBundle(coin_spends, G2Element()))])

    def close(self):
        return

    async def await_closed(self):
        await self.sim_client.await_closed()


async def get_node_and_wallet_clients(full_node_rpc_port: int, wallet_rpc_port: int, fingerprint: int):
    client = FullNodeClientMock(await _open_sim("./sim.db"))
    return client, WalletClientMock(client)


async def get_node_client(full_node_rpc_port: int):
    return FullNodeClientMock(await _open_sim("./sim.db"))


def get_additional_data():
    return DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
