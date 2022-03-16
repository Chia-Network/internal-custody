import click
import dataclasses
import os

from blspy import G1Element
from pathlib import Path
from typing import Optional

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint32, uint64

from cic import __version__
from cic.drivers.prefarm_info import PrefarmInfo
from cic.drivers.puzzle_root_construction import RootDerivation, calculate_puzzle_root

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


@click.group(
    help="\n  Commands to control a prefarm singleton \n",
    context_settings=CONTEXT_SETTINGS,
)
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context) -> None:
    ctx.ensure_object(dict)


@cli.command("init", short_help="Create a configuration file for the prefarm")
@click.option(
    "-f", "--filepath", help="The filepath at which to create the configuration file", default=".", required=True
)
@click.option("-d", "--date", help="Unix time at which withdrawals become possible", required=True)
@click.option("-r", "--rate", help="Mojos that can be withdrawn per second", required=True)
@click.option(
    "-a", "--amount", help="The initial amount that will be locked in this custody program (in mojos)", required=True
)
@click.option(
    "-wt",
    "--withdrawal-timelock",
    help="The amount of time where nothing has happened before a withdrawal can be made (in seconds)",
    required=True,
)
@click.option(
    "-rt",
    "--rekey-timelock",
    help="The amount of time where nothing has happened before a standard rekey can be initiated (in seconds)",
    required=True,
)
@click.option("-sp", "--slow-penalty", help="The time penalty for performing a slow rekey (in seconds)", required=True)
@click.option(
    "-pc",
    "--payment-clawback",
    help="The amount of time to clawback a payment before it's completed (in seconds)",
    required=True,
)
@click.option(
    "-rc",
    "--rekey-cancel",
    help="The amount of time to cancel a rekey before it's completed (in seconds)",
    required=True,
)
def init_cmd(
    filepath: str,
    date: int,
    rate: int,
    amount: int,
    withdrawal_timelock: int,
    rekey_timelock: int,
    slow_penalty: int,
    payment_clawback: int,
    rekey_cancel: int,
):
    prefarm_info = PrefarmInfo(
        bytes32([0] * 32),
        uint64(date),
        uint64(amount),
        uint64(rate),
        bytes32([0] * 32),
        uint64(withdrawal_timelock),
        uint64(payment_clawback),
        uint64(rekey_cancel),
        uint64(slow_penalty),
        uint64(rekey_timelock),
    )

    path = Path(filepath)
    if path.is_dir():
        path = path.joinpath("Configuration (needs derivation).txt")

    with open(path, "wb") as file:
        file.write(bytes(prefarm_info))


@cli.command("derive_root", short_help="Take an existing configuration and pubkey set to derive a puzzle root")
@click.option(
    "-c",
    "--configuration",
    help="The configuration file with which to derive the root",
    default="./Configuration (needs derivation).txt",
    required=True,
)
@click.option("-pks", "--pubkeys", help="A comma separated list of pubkeys to derive a puzzle for", required=True)
@click.option(
    "-m",
    "--initial-lock-level",
    help="The initial number of pubkeys required to do a withdrawal or standard rekey",
    required=True,
)
@click.option(
    "-n",
    "--maximum-lock-level",
    help="The maximum number of pubkeys required to do a withdrawal or standard rekey",
    required=False,
)
@click.option(
    "-min",
    "--minimum-pks",
    help="The minimum number of pubkeys required to initiate a slow rekey",
    default=1,
    required=True,
)
def derive_cmd(
    configuration: str,
    pubkeys: str,
    initial_lock_level: int,
    minimum_pks: int,
    maximum_lock_level: Optional[int] = None,
):
    with open(Path(configuration), "rb") as file:
        prefarm_info = PrefarmInfo.from_bytes(file.read())
    pubkeys: List[G1Element] = [G1Element.from_bytes(bytes.fromhex(pk)) for pk in pubkeys.split(",")]

    derivation: RootDerivation = calculate_puzzle_root(
        prefarm_info,
        pubkeys,
        uint32(initial_lock_level),
        uint32(len(pubkeys) if maximum_lock_level is None else maximum_lock_level),
        uint32(minimum_pks),
    )

    with open(Path(configuration), "wb") as file:
        file.write(bytes(derivation))
    if "needs derivation" in configuration:
        os.rename(Path(configuration), Path("awaiting launch".join(configuration.split("needs derivation"))))


def main() -> None:
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    main()
