import click

from cic import __version__

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


@click.group(
    help="\n  Commands to control a prefarm singleton \n",
    context_settings=CONTEXT_SETTINGS,
)
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context) -> None:
    ctx.ensure_object(dict)


# Example command

# @cli.command("test", short_help="Run the local test suite (located in ./tests)")
# @click.argument("tests", default="./tests", required=False)
# @click.option(
#     "-d",
#     "--discover",
#     is_flag=True,
#     help="List the tests without running them",
# )
# def test_cmd():
#     pass


def main() -> None:
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    main()
