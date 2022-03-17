import aiosqlite

from pathlib import Path
from chia.util.db_wrapper import DBWrapper

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
        await self.db_connection.commit()
        return self

    async def add_singleton_record(self, record: SingletonRecord):
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
