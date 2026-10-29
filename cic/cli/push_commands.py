import asyncio

from pathlib import Path
from typing import List, Optional

import click

from chia_rs import SpendBundle

from cic.cli.common import get_node_and_wallet_clients
from cic.cli.fees import (
    CANNOT_LINK_FEE_MESSAGE,
    FeeSpendError,
    bundle_fee,
    create_fee_spend_bundle,
    find_fee_announcement,
)


@click.command("push_tx", short_help="Push a signed spend bundle to the network")
@click.option(
    "-b",
    "--spend-bundle",
    help="The signed spend bundle",
    required=True,
)
@click.option(
    "-wp",
    "--wallet-rpc-port",
    help="Set the port where the Wallet is hosting the RPC interface. See the rpc_port under wallet in config.yaml",
    type=int,
    default=None,
)
@click.option("-f", "--fingerprint", help="Set the fingerprint to specify which wallet to use", type=int, default=None)
@click.option(
    "-np",
    "--node-rpc-port",
    help="Set the port where the Node is hosting the RPC interface. See the rpc_port under full_node in config.yaml",
    type=int,
    default=None,
)
@click.option(
    "-m",
    "--fee",
    help="Fee in mojos attached to the push.",
    type=int,
    default=0,
)
def push_cmd(
    spend_bundle: str,
    wallet_rpc_port: Optional[int],
    fingerprint: Optional[int],
    node_rpc_port: Optional[int],
    fee: int,
):
    async def do_command():
        try:
            node_client, wallet_client = await get_node_and_wallet_clients(node_rpc_port, wallet_rpc_port, fingerprint)

            try:
                if "." in spend_bundle:
                    with open(Path(spend_bundle), "r") as file:
                        spend_hex = file.read()
                else:
                    spend_hex = spend_bundle
                push_bundle = SpendBundle.from_bytes(bytes.fromhex(spend_hex))
            except Exception:
                print("Spend bundle cannot be recognized.  Please make sure this spend bundle is signed and try again.")
                return

            if fee > 0:
                existing_fee = bundle_fee(push_bundle)
                if existing_fee > 0:
                    raise click.ClickException(
                        f"This spend bundle already pays a fee of {existing_fee} mojos "
                        "(it already contains a fee transaction). "
                        f"Pushing it with --fee {fee} would pay a second fee. "
                        "Push it without --fee (or use --fee 0), or use the fee-less hex."
                    )

            spends: List[SpendBundle] = [push_bundle]

            if fee > 0:
                fee_announcement = find_fee_announcement(push_bundle)
                if fee_announcement is None:
                    raise click.ClickException(CANNOT_LINK_FEE_MESSAGE)
                try:
                    spends.append(await create_fee_spend_bundle(wallet_client, fee, fee_announcement))
                except FeeSpendError as error:
                    raise click.ClickException(
                        f"Could not create the fee transaction: {error}. Nothing was pushed. "
                        "Make sure the wallet has a spendable balance of at least the fee, or use --fee 0."
                    ) from error

            result = await node_client.push_tx(SpendBundle.aggregate(spends))
            print(result)

        finally:
            node_client.close()
            wallet_client.close()
            await node_client.await_closed()
            await wallet_client.await_closed()

    asyncio.run(do_command())
