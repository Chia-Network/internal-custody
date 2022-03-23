import asyncio

from blspy import G2Element
from typing import Any, Dict, List, Optional

from chia.clvm.spend_sim import SpendSim, SimClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.announcement import Announcement
from chia.types.coin_spend import CoinSpend
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint64

ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


class FullNodeClientMock(SimClient):
    # We're just overriding the push_tx endpoint to return a dict and farm a block when done
    async def push_tx(self, spend_bundle: SpendBundle) -> Dict[str, Any]:
        result = await super().push_tx(spend_bundle)
        if result[0] == MempoolInclusionStatus.SUCCESS:
            await self.service.farm_block()
            return {"success": True, "status": MempoolInclusionStatus.SUCCESS.name}
        else:
            return {"success": False, "error": result[1]}

    def close(self):
        return

    async def await_closed(self):
        await self.service.close()


class TXMock:
    def __init__(self, bundle):
        self.spend_bundle = bundle


class WalletClientMock:
    def __init__(self, sim_client):
        self.sim_client = sim_client

    # These are the only two methods we need
    async def select_coins(self, amount, wallet_id) -> List[Coin]:
        return [(await self.sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False))[0].coin]

    async def create_signed_transaction(
        self,
        additions: List[Dict],
        coins: Optional[List[Coin]] = None,
        fee=uint64(0),
        coin_announcements: Optional[List[Announcement]] = None,
    ) -> TXMock:
        total_amount: int = 0
        conditions: List[Program] = []
        for a in additions:
            total_amount += a["amount"]
            conditions.append(Program.to([51, a["puzzle_hash"], a["amount"]]))
        for a in coin_announcements:
            conditions.append(Program.to([61, a.name()]))
        if coins is None:
            coins = await self.select_coins(None, None)
        conditions.append(Program.to([51, ACS_PH, sum(c.amount for c in coins) - total_amount]))  # change
        coin_spends: List[CoinSpend] = [CoinSpend(coins[0], ACS, Program.to(conditions))]
        if len(coins) > 1:
            for coin in coins[1:]:
                coin_spends.append(CoinSpend(coin, ACS, Program.to([])))
        return TXMock(SpendBundle(coin_spends, G2Element()))

    def close(self):
        return

    async def await_closed(self):
        try:
            await self.sim_client.service.close()
        finally:
            return


async def get_wallet_and_node_clients(node_rpc_port: int, wallet_rpc_port: int, fingerprint: int):
    sim = await SpendSim.create(db_path="./sim.db")
    await sim.farm_block(ACS_PH)
    client = FullNodeClientMock(sim)
    return client, WalletClientMock(client)

async def get_wallet_client(wallet_rpc_port: int, fingerprint: int):
    sim = await SpendSim.create(db_path="./sim.db")
    await sim.farm_block(ACS_PH)
    client = FullNodeClientMock(sim)
    return WalletClientMock(client)

async def get_node_client(node_rpc_port: int):
    sim = await SpendSim.create(db_path="./sim.db")
    await sim.farm_block(ACS_PH)
    return FullNodeClientMock(sim)