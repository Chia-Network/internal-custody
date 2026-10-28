"""CLI-side orchestration for a two-step spend bundle push."""

import asyncio
import click
import time
from typing import Any, Dict, List, Optional, Tuple

from chia_rs import SpendBundle
from chia_rs.sized_bytes import bytes32

from cic.cli.fees import create_fee_spend_bundle, fee_condition_for_step, find_fee_announcement_and_spend
from cic.drivers.two_step_push import split_two_step_push


class TwoStepPushError(Exception):
    """Raised when one of the two push steps cannot be completed."""

    def __init__(
        self,
        message: str,
        second_bundle: SpendBundle,
        second_bundle_without_fee: Optional[SpendBundle] = None,
    ) -> None:
        self.second_bundle = second_bundle
        self.second_bundle_hex = bytes(second_bundle).hex()
        self.second_bundle_without_fee = second_bundle_without_fee
        if second_bundle_without_fee is not None and second_bundle_without_fee != second_bundle:
            message += (
                "\nStep 2 spend bundle without the fee (hex, retry with "
                "`cic push_tx --spend-bundle <hex> --fee N` to attach a new fee): "
                f"{bytes(second_bundle_without_fee).hex()}"
            )
        super().__init__(message)


def _second_bundle_description(second_bundle: SpendBundle) -> str:
    return f"Step 2 spend bundle (hex): {bytes(second_bundle).hex()}"


async def _coin_record_or_none(node_client: Any, coin_id: bytes32) -> Any:
    try:
        return await node_client.get_coin_record_by_name(coin_id)
    except ValueError:
        # The full node RPC raises when the coin is not known yet.
        return None


async def push_in_two_steps(
    node_client: Any,
    first_bundle: SpendBundle,
    second_bundle: SpendBundle,
    confirmation_timeout: float,
    poll_interval: float,
    second_bundle_without_fee: Optional[SpendBundle] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Push a bundle in two steps.

    Step 1 is pushed first.  Then the coin records for every coin spent by step 2
    are polled with ``get_coin_record_by_name`` until all of them exist (that is,
    they are confirmed) or ``confirmation_timeout`` seconds have elapsed since
    the start of the wait.  A timeout of 0 still performs one check before
    raising.  Once every step 2 coin is confirmed, step 2 is pushed.  Its
    ephemeral spends need no signature of their own; when a fee was attached,
    it also carries the step 2 fee transaction's signature.

    If step 1 is rejected, nothing is pushed and step 2 is not attempted.  If the
    wait times out or step 2 is rejected, the raised ``TwoStepPushError`` carries
    the second bundle so it can be pushed later with ``cic push_tx``.  When a fee
    was attached, it also carries the step 2 bundle without that fee so a new
    fee can be attached to a retry.
    """
    first_result = await node_client.push_tx(first_bundle)
    if not first_result.get("success"):
        raise TwoStepPushError(
            "Step 1 of the two-step push was rejected by the node: "
            f"{first_result.get('error')}. Step 2 was not attempted. "
            f"{_second_bundle_description(second_bundle)}",
            second_bundle,
            second_bundle_without_fee,
        )

    coin_ids: List[bytes32] = []
    seen_coin_ids = set()
    for coin_spend in second_bundle.coin_spends:
        coin_id = coin_spend.coin.name()
        if coin_id not in seen_coin_ids:
            seen_coin_ids.add(coin_id)
            coin_ids.append(coin_id)

    start = time.monotonic()
    while True:
        records = [await _coin_record_or_none(node_client, coin_id) for coin_id in coin_ids]
        if all(record is not None for record in records):
            break
        if time.monotonic() - start >= confirmation_timeout:
            raise TwoStepPushError(
                "Step 1 was accepted by the node into its mempool, but the coins spent in step 2 were "
                f"not seen confirmed within {confirmation_timeout} seconds. Step 1 may have been dropped "
                "or may still be waiting to be confirmed. Verify with `cic sync` (or a block explorer) "
                "that the lock coin and the new singleton coin exist before pushing step 2; a step 2 "
                "pushed before they exist will be rejected. The second part can be pushed later with "
                f"`cic push_tx`. {_second_bundle_description(second_bundle)}",
                second_bundle,
                second_bundle_without_fee,
            )
        await asyncio.sleep(poll_interval)

    second_result = await node_client.push_tx(second_bundle)
    if not second_result.get("success"):
        raise TwoStepPushError(
            "Step 1 was accepted, but step 2 was rejected by the node: "
            f"{second_result.get('error')}. The second part can be retried with `cic push_tx`. "
            f"{_second_bundle_description(second_bundle)}",
            second_bundle,
            second_bundle_without_fee,
        )

    return first_result, second_result


async def run_two_step_push(
    node_client: Any,
    wallet_client: Any,
    push_bundle: SpendBundle,
    fee: int,
    confirmation_timeout: float,
    poll_interval: float,
) -> None:
    """Push a bundle that needs a two-step push, attaching a fee to both steps.

    When ``fee`` is greater than zero, BOTH steps carry a fee transaction of
    ``fee`` mojos each, so ``2 * fee`` mojos are paid in total.  The step that
    contains the announcement spend binds its fee with an
    ``AssertCoinAnnouncement``; the other step binds its fee with an
    ``AssertConcurrentSpend`` on its first coin spend.
    """
    two_step_bundles = split_two_step_push(push_bundle)
    first_bundle = two_step_bundles.first
    second_bundle = two_step_bundles.second
    second_bundle_without_fee: Optional[SpendBundle] = None

    if fee > 0:
        # Find the announcement with the original last-match rule over the whole
        # bundle.  The step that holds the announcement spend keeps the
        # announcement binding; the other step gets an AssertConcurrentSpend on
        # its first coin spend so its fee is pushed atomically with that step.
        fee_match = find_fee_announcement_and_spend(push_bundle)
        if fee_match is None:
            print("Cannot find a way to link fee to this transaction. Please specify 0 fee and try again.")
            return
        announcement_spend, fee_announcement = fee_match
        second_bundle_without_fee = second_bundle

        # The wallet is called with push=False, so it does not reserve the coins
        # selected for a fee.  Build both fee bundles before pushing anything: a
        # wallet failure then never leaves a half-pushed lock.  Build step 1's fee
        # first and exclude its coins from step 2's fee, or the wallet would select
        # the same coin for both and step 1 would make step 2 a double spend.
        first_fee_condition = fee_condition_for_step(first_bundle, announcement_spend, fee_announcement)
        first_fee_bundle = await create_fee_spend_bundle(wallet_client, fee, first_fee_condition)
        first_fee_coin_ids = [coin_spend.coin.name() for coin_spend in first_fee_bundle.coin_spends]
        second_fee_condition = fee_condition_for_step(second_bundle, announcement_spend, fee_announcement)
        second_fee_bundle = await create_fee_spend_bundle(
            wallet_client,
            fee,
            second_fee_condition,
            excluded_coin_ids=first_fee_coin_ids,
        )

        second_fee_coin_ids = {coin_spend.coin.name() for coin_spend in second_fee_bundle.coin_spends}
        overlapping_coin_ids = set(first_fee_coin_ids) & second_fee_coin_ids
        if overlapping_coin_ids:
            raise click.ClickException(
                "The two fee transactions spend the same coin(s): "
                f"{', '.join(sorted(coin_id.hex() for coin_id in overlapping_coin_ids))}. "
                "Nothing was pushed; retry with a larger wallet balance or a different fee."
            )

        first_bundle = SpendBundle.aggregate([first_bundle, first_fee_bundle])
        second_bundle = SpendBundle.aggregate([second_bundle, second_fee_bundle])

    # A lock level increase creates the lock coin and the new singleton coin inside the
    # same bundle.  Chia 2.7.5 rejects relative time conditions on such ephemeral spends,
    # so step 1 confirms those coins first and step 2 spends them afterwards.  Step 2 needs
    # no signature (unless it carries the fee).  If step 2 never lands, the lock coin stays
    # pending on chain and the printed step 2 bundle can be pushed later with `cic push_tx`.
    print("This spend bundle must be pushed in two steps.")
    print("Step 1: non-ephemeral spends (the START_REKEY spend and any non-ephemeral fee).")
    print("Step 2: ephemeral spends (the lock coin completion, FINISH_REKEY, and any ephemeral fee).")
    try:
        first_result, second_result = await push_in_two_steps(
            node_client,
            first_bundle,
            second_bundle,
            confirmation_timeout,
            poll_interval,
            second_bundle_without_fee,
        )
    except TwoStepPushError as error:
        raise click.ClickException(str(error))
    print(first_result)
    print(second_result)
