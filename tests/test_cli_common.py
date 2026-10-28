import asyncio

from pathlib import Path
from typing import List

import pytest

from cic.cli.common import open_sync_store
from cic.cli.sync_store import SyncStore


def test_open_sync_store_closes_db_when_body_raises(tmp_path: Path) -> None:
    """The sync store wrapper is closed when the body raises."""
    db_path = tmp_path / "sync.sqlite"
    opened: List[SyncStore] = []

    async def use_store() -> None:
        async with open_sync_store(str(db_path)) as sync_store:
            opened.append(sync_store)
            raise RuntimeError("body failed")

    with pytest.raises(RuntimeError, match="body failed"):
        asyncio.run(use_store())

    assert len(opened) == 1
    assert opened[0].db_wrapper._write_connection._connection is None
