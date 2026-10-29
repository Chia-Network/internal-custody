"""Shared helpers for tests that build and submit payment series."""

from typing import List, Optional, Sequence, Tuple

from chia._tests.util.spend_sim import SimClient, SpendSim
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.util.errors import Err
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_offset,
)
from chia_rs import G2Element, PrivateKey, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia_base.bls12_381 import BLSPublicKey, BLSSecretExponent
from hsms.core.signing_hints import SumHint
from hsms.core.unsigned_spend import UnsignedSpend

from cic.cli.common import to_hsm_coin_spend, write_unsigned_spend
from cic.drivers.prefarm import construct_singleton_inner_puzzle, get_withdrawal_spend_info
from cic.drivers.puzzle_root_construction import RootDerivation
from cic.drivers.singleton import construct_p2_singleton

from tests.test_lifecycle import ACS, ACS_PH, secret_key_for_index


def make_payment(
    singleton: Coin,
    lineage: LineageProof,
    derivation: RootDerivation,
    amount: int,
    p2_singletons: Optional[List[Coin]] = None,
) -> Tuple[SpendBundle, bytes]:
    """Build an unsigned payment spend for the given singleton state."""
    pubkeys = [secret_key_for_index(index).get_g1() for index in (0, 1, 2)]
    return get_withdrawal_spend_info(
        singleton,
        pubkeys,
        derivation,
        lineage,
        uint64(amount),
        ACS_PH,
        p2_singletons_to_claim=[] if p2_singletons is None else p2_singletons,
    )


def expected_next(
    current: Coin,
    amount: int,
    absorbed_sum: int,
    derivation: RootDerivation,
) -> Tuple[Coin, LineageProof]:
    """Compute the singleton coin and lineage proof that a payment leaves behind."""
    next_coin = Coin(current.name(), current.puzzle_hash, uint64(int(current.amount) - amount + absorbed_sum))
    next_lineage = LineageProof(
        current.parent_coin_info,
        construct_singleton_inner_puzzle(derivation.prefarm_info).get_tree_hash(),
        current.amount,
    )
    return next_coin, next_lineage


def bundle_to_unsigned_spend_file(
    path: str,
    bundle: SpendBundle,
    key_indices: Sequence[int] = (0, 1, 2),
) -> None:
    """Write a spend bundle in the unsigned spend file format used by the CLI."""
    pubkeys = [secret_key_for_index(index).get_g1() for index in key_indices]
    as_bls_pubkey_list = [BLSPublicKey(pubkey) for pubkey in pubkeys]
    aggregate_pubkey = sum(as_bls_pubkey_list, start=BLSPublicKey.zero())
    synthetic_secret = BLSSecretExponent(
        PrivateKey.from_bytes(
            calculate_synthetic_offset(aggregate_pubkey._g1, DEFAULT_HIDDEN_PUZZLE_HASH).to_bytes(32, "big")
        )
    )
    coin_spends = [to_hsm_coin_spend(coin_spend) for coin_spend in bundle.coin_spends]
    unsigned_spend = UnsignedSpend(
        coin_spends,
        [SumHint(as_bls_pubkey_list, synthetic_secret)],
        [],
        DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA,
    )
    write_unsigned_spend(path, unsigned_spend)


async def push_bundle(sim_client: SimClient, bundle: SpendBundle) -> Tuple[MempoolInclusionStatus, Optional[Err]]:
    """Push a bundle to the simulated mempool and return its status and error."""
    return await sim_client.push_tx(bundle)


async def pass_time_and_farm(sim: SpendSim, seconds: int) -> None:
    """Advance the simulated clock and farm one block."""
    sim.pass_time(uint64(seconds))
    await sim.farm_block()


async def create_p2_singleton_coins(
    sim: SpendSim,
    sim_client: SimClient,
    launcher_id: bytes32,
    amounts: Sequence[int],
) -> List[Coin]:
    """Create p2_singleton coins with the given amounts from an available ACS coin."""
    await sim.farm_block(ACS_PH)
    records = await sim_client.get_coin_records_by_puzzle_hashes([ACS_PH], include_spent_coins=False)
    total = sum(amounts)
    funding_coin = next(record.coin for record in records if record.coin.amount > total)
    p2_puzzle_hash = construct_p2_singleton(launcher_id).get_tree_hash()
    conditions = [[51, p2_puzzle_hash, amount] for amount in amounts]
    conditions.append([51, ACS_PH, int(funding_coin.amount) - total])
    bundle = SpendBundle([make_spend(funding_coin, ACS, Program.to(conditions))], G2Element())
    await sim_client.push_tx(bundle)
    await sim.farm_block()
    created = await sim_client.get_coin_records_by_puzzle_hashes([p2_puzzle_hash], include_spent_coins=False)
    return [record.coin for record in created if record.coin.amount in amounts]
