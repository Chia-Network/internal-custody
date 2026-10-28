"""Helpers for finding a fee announcement and building a linked fee spend bundle."""

from typing import Any, List, Optional, Tuple

from chia_rs import SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64
from chia.types.blockchain_format.program import Program, INFINITE_COST
from chia.types.coin_spend import CoinSpend
from chia.wallet.conditions import AssertCoinAnnouncement
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG
from chia.wallet.wallet_request_types import Addition, CreateSignedTransaction
from clvm.casts import int_to_bytes


def find_fee_announcement_and_spend(
    spend_bundle: SpendBundle,
) -> Optional[Tuple[CoinSpend, AssertCoinAnnouncement]]:
    """Return the last coin spend that creates a coin announcement, with its assertion."""
    result: Optional[Tuple[CoinSpend, AssertCoinAnnouncement]] = None
    for coin_spend in spend_bundle.coin_spends:
        puzzle_reveal = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
        solution = Program.from_bytes(bytes(coin_spend.solution))
        _, conditions = puzzle_reveal.run_with_cost(INFINITE_COST, solution)
        for condition in conditions.as_python():
            if condition[0] == int_to_bytes(60):  # CREATE_COIN_ANNOUNCEMENT
                result = (
                    coin_spend,
                    AssertCoinAnnouncement(
                        asserted_id=coin_spend.coin.name(),
                        asserted_msg=condition[1],
                    ),
                )
                break
    return result


def find_fee_announcement(spend_bundle: SpendBundle) -> Optional[AssertCoinAnnouncement]:
    """Return the last coin announcement in the bundle, if any."""
    result = find_fee_announcement_and_spend(spend_bundle)
    return None if result is None else result[1]


async def create_fee_spend_bundle(
    wallet_client: Any,
    fee: int,
    fee_announcement: Optional[AssertCoinAnnouncement],
) -> SpendBundle:
    """Create a fee spend bundle, optionally asserting the given announcement."""
    extra_conditions: tuple = () if fee_announcement is None else (fee_announcement,)
    tx_response = await wallet_client.create_signed_transactions(
        CreateSignedTransaction(
            wallet_id=uint32(1),
            additions=[Addition(puzzle_hash=bytes32([0] * 32), amount=uint64(0))],
            fee=uint64(fee),
            push=False,
            sign=True,
        ),
        DEFAULT_TX_CONFIG,
        extra_conditions,
    )
    txs: List[TransactionRecord] = tx_response.signed_txs
    if len(txs) == 0:
        raise ValueError("`create_signed_transaction` returned empty list!")

    tx = txs[0]
    if tx.spend_bundle is None:
        raise ValueError("`create_signed_transaction` returned an unsigned transaction!")
    return tx.spend_bundle
