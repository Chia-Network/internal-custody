import click

from pathlib import Path

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64

from cic import __version__
from cic.drivers.prefarm_info import PrefarmInfo

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
        path = path.joinpath("Public Info (needs derivation).txt")

    with open(path, "wb") as file:
        file.write(bytes(prefarm_info))

def main() -> None:
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    main()
