"""Helpers for finding a fee announcement and building a linked fee spend bundle."""

from typing import Any, Iterable, List, Optional, Tuple

from chia_rs import SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64
from chia.types.blockchain_format.program import Program, INFINITE_COST
from chia.types.coin_spend import CoinSpend
from chia.wallet.conditions import AssertCoinAnnouncement, AssertConcurrentSpend, Condition
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG
from chia.wallet.wallet_request_types import Addition, CreateSignedTransaction
from clvm.casts import int_to_bytes

CANNOT_LINK_FEE_MESSAGE = "Cannot find a way to link fee to this transaction. Please specify 0 fee and try again."


class FeeSpendError(Exception):
    """Raised when the wallet cannot fund or build a fee transaction."""


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


def bundle_fee(spend_bundle: SpendBundle) -> int:
    """Return the fee a spend bundle pays: spent coins minus created coins.

    An ephemeral coin that is both created and spent inside the bundle appears on
    both sides and therefore cancels out.
    """
    return sum(coin.amount for coin in spend_bundle.removals()) - sum(coin.amount for coin in spend_bundle.additions())


def fee_condition_for_step(
    step_bundle: SpendBundle,
    announcement_spend: CoinSpend,
    announcement: AssertCoinAnnouncement,
) -> Condition:
    """Return the condition binding a fee transaction to ``step_bundle``.

    The step that contains the announcement spend asserts the announcement itself.
    The other step asserts that its first coin spend happens concurrently with the
    fee spend, which keeps the fee in the same push as the rest of that step.
    """
    announcement_coin_id = announcement_spend.coin.name()
    if any(coin_spend.coin.name() == announcement_coin_id for coin_spend in step_bundle.coin_spends):
        return announcement
    if len(step_bundle.coin_spends) == 0:
        raise ValueError("Cannot link a fee to an empty step of a two-step push")
    return AssertConcurrentSpend(step_bundle.coin_spends[0].coin.name())


async def create_fee_spend_bundle(
    wallet_client: Any,
    fee: int,
    condition: Optional[Condition],
    excluded_coin_ids: Iterable[bytes32] = (),
) -> SpendBundle:
    """Create a fee spend bundle, optionally asserting the given condition.

    ``excluded_coin_ids`` keeps the wallet from selecting coins that are already
    spent by another fee transaction.  The wallet is called with ``push=False``,
    so it does not reserve the selected coins itself.

    Wallet failures are reported as ``FeeSpendError`` so callers can explain what
    happened without leaking a raw wallet exception to the CLI user.
    """
    extra_conditions: tuple = () if condition is None else (condition,)
    excluded_ids: List[bytes32] = list(excluded_coin_ids)
    tx_config = DEFAULT_TX_CONFIG
    if excluded_ids:
        tx_config = DEFAULT_TX_CONFIG.override(excluded_coin_ids=excluded_ids)
    try:
        tx_response = await wallet_client.create_signed_transactions(
            CreateSignedTransaction(
                wallet_id=uint32(1),
                additions=[Addition(puzzle_hash=bytes32([0] * 32), amount=uint64(0))],
                fee=uint64(fee),
                push=False,
                sign=True,
            ),
            tx_config,
            extra_conditions,
        )
        txs: List[TransactionRecord] = tx_response.signed_txs
        if len(txs) == 0:
            raise ValueError("`create_signed_transaction` returned empty list!")
        tx = txs[0]
        if tx.spend_bundle is None:
            raise ValueError("`create_signed_transaction` returned an unsigned transaction!")
    except Exception as error:
        raise FeeSpendError(str(error)) from error
    return tx.spend_bundle
