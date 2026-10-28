import click

from cic import __version__
from cic.cli.common import CONTEXT_SETTINGS, read_unsigned_spend  # noqa: F401
from cic.cli.config_commands import (
    derive_cmd,
    export_cmd,
    init_cmd,
    launch_cmd,
    update_cmd,
)
from cic.cli.examine_commands import examine_cmd, which_pubkeys_cmd
from cic.cli.payment_commands import clawback_cmd, complete_cmd, payments_cmd
from cic.cli.push_commands import push_cmd
from cic.cli.rekey_commands import increase_cmd, start_rekey_cmd
from cic.cli.series_commands import check_series_cmd
from cic.cli.sync_commands import address_cmd, audit_cmd, show_cmd, sync_cmd


@click.group(
    help="\n  Commands to control a prefarm singleton \n",
    context_settings=CONTEXT_SETTINGS,
)
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context) -> None:
    ctx.ensure_object(dict)


cli.add_command(init_cmd)
cli.add_command(derive_cmd)
cli.add_command(launch_cmd)
cli.add_command(update_cmd)
cli.add_command(export_cmd)
cli.add_command(sync_cmd)
cli.add_command(address_cmd)
cli.add_command(push_cmd)
cli.add_command(payments_cmd)
cli.add_command(check_series_cmd)
cli.add_command(start_rekey_cmd)
cli.add_command(clawback_cmd)
cli.add_command(complete_cmd)
cli.add_command(increase_cmd)
cli.add_command(show_cmd)
cli.add_command(audit_cmd)
cli.add_command(examine_cmd)
cli.add_command(which_pubkeys_cmd)


def main() -> None:
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    main()
