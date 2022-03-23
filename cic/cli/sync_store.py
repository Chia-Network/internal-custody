import aiosqlite

from blspy import G1Element
from pathlib import Path
from typing import List, Optional

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import SerializedProgram
from chia.util.db_wrapper import DBWrapper
from chia.util.ints import uint32, uint64
from chia.wallet.lineage_proof import LineageProof

from cic.cli.singleton_record import SingletonRecord


class SyncStore:
    db_connection: aiosqlite.Connection
    db_wrapper: DBWrapper

    @classmethod
    async def create(cls, db_path: Path):
        self = cls()

        wrapper = DBWrapper(await aiosqlite.connect(db_path))
        self.db_connection = wrapper.db
        self.db_wrapper = wrapper

        await self.db_connection.execute(
            "CREATE TABLE IF NOT EXISTS singletons("
            "   coin_id blob PRIMARY_KEY,"
            "   parent_id blob,"
            "   puzzle_hash blob,"
            "   amount bigint,"
            "   puzzle_root blob,"
            "   lineage_proof blob,"
            "   generation bigint,"
            "   puzzle_reveal blob,"
            "   solution blob,"
            "   spend_type int,"
            "   spending_pubkey blob"
            ")"
        )

        await self.db_connection.execute(
            "CREATE TABLE IF NOT EXISTS p2_singletons("
            "   coin_id blob PRIMARY_KEY,"
            "   parent_id blob,"
            "   puzzle_hash blob,"
            "   amount bigint,"
            "   spent tinyint"
            ")"
        )
        await self.db_connection.commit()
        return self

    async def add_singleton_record(self, record: SingletonRecord) -> None:
        cursor = await self.db_connection.execute(
            "INSERT OR REPLACE INTO singletons VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.coin.name(),
                record.coin.parent_coin_info,
                record.coin.puzzle_hash,
                record.coin.amount,
                record.puzzle_root,
                bytes(record.lineage_proof),
                record.generation,
                bytes([0]) if record.puzzle_reveal is None else bytes(record.puzzle_reveal),
                bytes([0]) if record.solution is None else bytes(record.solution),
                0 if record.spend_type is None else bytes(record.spend_type),
                bytes([0]) if record.spending_pubkey is None else bytes(record.spending_pubkey),
            ),
        )
        await cursor.close()

    async def add_p2_singletons(self, coins: List[Coin]) -> None:
        for p2_singleton in coins:
            cursor = await self.db_connection.execute(
                "INSERT OR REPLACE INTO p2_singletons VALUES(?, ?, ?, ?, ?)",
                (
                    p2_singleton.name(),
                    p2_singleton.parent_coin_info,
                    p2_singleton.puzzle_hash,
                    p2_singleton.amount,
                    0,
                ),
            )
        if len(coins) > 0:
            await cursor.close()

    async def get_latest_singleton(self) -> Optional[SingletonRecord]:
        cursor = await self.db_connection.execute("SELECT * from singletons ORDER BY generation DESC LIMIT 1")
        record = await cursor.fetchone()
        await cursor.close()
        return SingletonRecord(
            Coin(
                record[1],
                record[2],
                uint64(record[3]),
            ),
            record[4],
            LineageProof.from_bytes(record[5]),
            uint32(record[6]),
            None if record[7] == bytes([0]) else SerializedProgram.from_bytes(record[7]),
            None if record[8] == bytes([0]) else SerializedProgram.from_bytes(record[8]),
            None if record[9] == 0 else SpendType(record[9]),
            None if record[10] == bytes([0]) else G1Element.from_bytes(record[10]),
        ) if record is not None else None

    async def get_p2_singletons(self, minimum_amount = uint64(0), max_num: Optional[uint32] = None) -> List[Coin]:
        limit_str: str = f" LIMIT {max_num}" if max_num is not None else ""
        cursor = await self.db_connection.execute(
            f"SELECT * from p2_singletons WHERE amount>=? AND spent==0 ORDER BY amount DESC{limit_str}", (minimum_amount,)
        )
        coins = await cursor.fetchall()
        await cursor.close()

        return [Coin(coin[1], coin[2], uint64(coin[3])) for coin in coins]