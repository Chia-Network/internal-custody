import os

from pathlib import Path
from typing import Iterable, Optional

from chia.types.coin_spend import CoinSpend
from chia_rs import G1Element
from chia_rs.sized_bytes import bytes32

from cic.cli.sync_store import SyncStore
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation

from chia_base.bls12_381 import BLSPublicKey
from hsms.core.unsigned_spend import UnsignedSpend
from chia_base.atoms import bytes32 as HSMbytes32
from chia_base.atoms import uint64 as HSMuint64
from chia_base.core import Coin as HSMCoin
from chia_base.core import CoinSpend as HSMCoinSpend
from clvm_rs import Program as CProgram
from hsms.util.qrint_encoding import a2b_qrint, b2a_qrint

if os.environ.get("TESTING_CIC_CLI", "FALSE") == "TRUE":
    from tests.cli_clients import get_node_and_wallet_clients, get_node_client, get_additional_data  # noqa: F401
else:
    from cic.cli.clients import get_node_and_wallet_clients, get_node_client, get_additional_data  # noqa: F401

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


def to_hsm_coin_spend(coin_spend: CoinSpend) -> HSMCoinSpend:
    return HSMCoinSpend(
        HSMCoin(
            HSMbytes32(bytes(coin_spend.coin.parent_coin_info)),
            HSMbytes32(bytes(coin_spend.coin.puzzle_hash)),
            HSMuint64(int(coin_spend.coin.amount)),
        ),
        CProgram.from_bytes(bytes(coin_spend.puzzle_reveal)),
        CProgram.from_bytes(bytes(coin_spend.solution)),
    )


def load_prefarm_info(configuration: Optional[str]) -> PrefarmInfo:
    if configuration is None:
        path: Path = next(Path("./").glob("Configuration (*).txt"))
    else:
        path = Path(configuration)
        if path.is_dir():
            path = next(path.glob("Configuration (*).txt"))
    with open(path, "rb") as file:
        file_bytes = file.read()
        try:
            return PrefarmInfo.from_bytes(file_bytes)
        except (AssertionError, ValueError):
            try:
                return RootDerivation.from_bytes(file_bytes).prefarm_info
            except (AssertionError, ValueError):
                raise ValueError("The configuration specified is not a recognizable format")


def load_root_derivation(configuration: Optional[str]) -> RootDerivation:
    if configuration is None:
        path: Path = next(Path("./").glob("Configuration (*).txt"))
    else:
        path = Path(configuration)
        if path.is_dir():
            path = next(path.glob("Configuration (*).txt"))
    with open(path, "rb") as file:
        file_bytes = file.read()
    try:
        return RootDerivation.from_bytes(file_bytes)
    except (AssertionError, ValueError):
        pass
    try:
        PrefarmInfo.from_bytes(file_bytes)
    except (AssertionError, ValueError):
        raise ValueError("The configuration specified is not a recognizable format")
    raise ValueError("The specified configuration file can only perform observer actions")


async def load_db(db_path: str, launcher_id: Optional[bytes32] = None) -> SyncStore:
    path = Path(db_path)
    if path.is_dir():
        existing = list(path.glob("sync (*).sqlite"))
        if len(existing) == 0:
            if launcher_id is None:
                raise ValueError("Insufficient info to initialize DB")
            else:
                path = path.joinpath(f"sync ({launcher_id[0:3].hex()}).sqlite")
        else:
            path = existing[0]
    return await SyncStore.create(path)


def load_pubkeys(pubkey_files_str: str) -> Iterable[G1Element]:
    for filepath in pubkey_files_str.split(","):
        with open(Path(filepath), "r") as file:
            yield BLSPublicKey.from_bech32m(file.read().strip())._g1


def write_unsigned_spend(filename: str, spend: UnsignedSpend) -> None:
    with open(filename, "w") as file:
        file.write(b2a_qrint(bytes(spend)))


def read_unsigned_spend(filename: str) -> UnsignedSpend:
    with open(filename, "r") as file:
        return UnsignedSpend.from_bytes(a2b_qrint(file.read()))
