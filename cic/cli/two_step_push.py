"""CLI-side orchestration for a two-step spend bundle push."""

import asyncio
import click
import time
from typing import Any, Dict, List, Tuple

from chia_rs import SpendBundle
from chia_rs.sized_bytes import bytes32

from cic.cli.fees import create_fee_spend_bundle, find_fee_announcement_and_spend
from cic.drivers.two_step_push import split_two_step_push


class TwoStepPushError(Exception):
    """Raised when one of the two push steps cannot be completed."""

    def __init__(self, message: str, second_bundle: SpendBundle) -> None:
        super().__init__(message)
        self.second_bundle = second_bundle
        self.second_bundle_hex = bytes(second_bundle).hex()


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
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Push a bundle in two steps.

    Step 1 is pushed first.  Then the coin records for every coin spent by step 2
    are polled with ``get_coin_record_by_name`` until all of them exist (that is,
    they are confirmed) or ``confirmation_timeout`` seconds have elapsed since
    the start of the wait.  A timeout of 0 still performs one check before
    raising.  Once every step 2 coin is confirmed, step 2 is pushed.  Its
    ephemeral spends need no signature; when a fee transaction is aggregated
    into step 2, it also carries the fee transaction's signature.

    If step 1 is rejected, nothing is pushed and step 2 is not attempted.  If the
    wait times out or step 2 is rejected, the raised ``TwoStepPushError`` carries
    the second bundle so it can be pushed later with ``cic push_tx``.
    """
    first_result = await node_client.push_tx(first_bundle)
    if not first_result.get("success"):
        raise TwoStepPushError(
            "Step 1 of the two-step push was rejected by the node: "
            f"{first_result.get('error')}. Step 2 was not attempted. "
            f"{_second_bundle_description(second_bundle)}",
            second_bundle,
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
                "Step 1 was accepted, but the coins spent in step 2 were not seen confirmed within "
                f"{confirmation_timeout} seconds. The second part can be pushed later with `cic push_tx`. "
                f"{_second_bundle_description(second_bundle)}",
                second_bundle,
            )
        await asyncio.sleep(poll_interval)

    second_result = await node_client.push_tx(second_bundle)
    if not second_result.get("success"):
        raise TwoStepPushError(
            "Step 1 was accepted, but step 2 was rejected by the node: "
            f"{second_result.get('error')}. The second part can be retried with `cic push_tx`. "
            f"{_second_bundle_description(second_bundle)}",
            second_bundle,
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
    """Push a bundle that needs a two-step push, linking any fee to its announcement spend."""
    two_step_bundles = split_two_step_push(push_bundle)
    first_bundle = two_step_bundles.first
    second_bundle = two_step_bundles.second

    if fee > 0:
        # Find the announcement with the original last-match rule over the whole
        # bundle, then put the fee transaction in the same step as the spend that
        # emits it.  A lock level increase emits its only coin announcement from the
        # lock coin completion spend, which is an ephemeral spend and therefore part
        # of step 2.  Step 2 then carries the fee, so the fee assertion and the
        # announcement are pushed atomically.  If step 2 has to be retried manually,
        # the printed step 2 hex can be pushed again with `cic push_tx --fee N`,
        # which attaches a fee in the ordinary single-push way.
        fee_match = find_fee_announcement_and_spend(push_bundle)
        if fee_match is None:
            print("Cannot find a way to link fee to this transaction. Please specify 0 fee and try again.")
            return
        announcement_spend, fee_announcement = fee_match
        fee_spend_bundle = await create_fee_spend_bundle(wallet_client, fee, fee_announcement)
        if announcement_spend.coin.name() in {coin_spend.coin.name() for coin_spend in first_bundle.coin_spends}:
            first_bundle = SpendBundle.aggregate([first_bundle, fee_spend_bundle])
        else:
            second_bundle = SpendBundle.aggregate([second_bundle, fee_spend_bundle])

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
        )
    except TwoStepPushError as error:
        raise click.ClickException(str(error))
    print(first_result)
    print(second_result)
