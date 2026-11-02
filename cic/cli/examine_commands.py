import itertools
import os
import segno
import tempfile
import webbrowser

from pathlib import Path
from typing import Dict, List, Optional

import click

from chia.types.blockchain_format.program import Program
from chia_rs import PrivateKey, G1Element
from chia_rs.sized_bytes import bytes32
from chia.util.bech32m import encode_puzzle_hash
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    calculate_synthetic_offset,
    DEFAULT_HIDDEN_PUZZLE_HASH,
)

from cic.cli.common import load_pubkeys, load_root_derivation, read_unsigned_spend
from cic.drivers.prefarm import (
    get_puzzle_root_from_puzzle,
    get_spend_type_for_solution,
    get_spending_pubkey_for_solution,
    get_spending_pubkey_for_drop_coin,
    get_spend_params_for_ach_creation,
    get_spend_params_for_rekey_creation,
    get_info_for_ach_drop,
    get_info_for_rekey_drop,
)
from cic.drivers.puzzle_root_construction import calculate_puzzle_root

from chia_base.atoms import bytes32 as HSMbytes32
from chia_base.bls12_381 import BLSPublicKey
from chia_base.core import CoinSpend as HSMCoinSpend
from hsms.util.byte_chunks import chunks_for_zlib_blob
from hsms.util.qrint_encoding import b2a_qrint


@click.command("examine_spend", short_help="Examine an unsigned spend to see the details before you sign it")
@click.argument("spend_file", nargs=1, required=True)
@click.option(
    "--qr-density", help="The amount of bytes to pack into a single QR code", default=250, show_default=True, type=int
)
@click.option(
    "-va",
    "--validate-against",
    help="A new configuration file to check against requests for rekeys",
    required=False,
    default=None,
)
def examine_cmd(
    spend_file: str,
    qr_density: int,
    validate_against: str,
):
    bundle = read_unsigned_spend(spend_file)

    singleton_spends: List[HSMCoinSpend] = [cs for cs in bundle.coin_spends if cs.coin.amount % 2 == 1]
    drop_coin_spends: List[HSMCoinSpend] = [cs for cs in bundle.coin_spends if cs.coin.amount % 2 == 0]
    if len(singleton_spends) > 1:
        names: List[HSMbytes32] = [cs.coin.name() for cs in singleton_spends]
        spend: HSMCoinSpend = next(cs for cs in singleton_spends if cs.coin.parent_coin_info not in names)
        spend_type: str = "LOCK"
    elif len(singleton_spends) == 1:
        spend = singleton_spends[0]
        spend_type = get_spend_type_for_solution(Program.from_bytes(bytes(spend.solution))).name
    else:
        spend = drop_coin_spends[0]
        if spend.coin.amount == 0:
            spend_type = "REKEY_CANCEL"
        else:
            spend_type = "PAYMENT_CLAWBACK"

    # HSM type conversions
    puzzle = Program.from_bytes(bytes(spend.puzzle_reveal))
    solution = Program.from_bytes(bytes(spend.solution))

    spend_summary: str
    if spend_type == "HANDLE_PAYMENT":
        spending_pubkey = get_spending_pubkey_for_solution(solution)
        assert spending_pubkey is not None
        out_amount, in_amount, p2_ph = get_spend_params_for_ach_creation(solution)
        print("Type: Payment")
        print(f"Incoming: {in_amount}")
        print(f"Outgoing: {out_amount}")
        print(f"To: {encode_puzzle_hash(p2_ph, 'xch')}")
        print(f"Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}")
        spend_summary = f"""
        <div>
          <ul>
            <li>Type: Payment</li>
            <li>Incoming: {in_amount}</li>
            <li>Outgoing: {out_amount}</li>
            <li>To: {encode_puzzle_hash(p2_ph, 'xch')}</li>
            <li>Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}</li>
          </ul>
        </div>
        """
    elif spend_type == "LOCK":
        spending_pubkey = get_spending_pubkey_for_solution(solution)
        assert spending_pubkey is not None
        print("Type: Lock level increase")
        print(f"Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}")
        spend_summary = f"""
        <div>
          <ul>
            <li>Type: Lock level increase</li>
            <li>Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}</li>
          </ul>
        </div>
        """
    elif spend_type == "START_REKEY":
        spending_pubkey = get_spending_pubkey_for_solution(solution)
        assert spending_pubkey is not None
        from_root = get_puzzle_root_from_puzzle(puzzle)
        timelock, new_root = get_spend_params_for_rekey_creation(solution)
        print("Type: Rekey")
        print(f"From: {from_root}")
        print(f"To: {new_root}")
        print(f"Slow factor: {timelock}")
        print(f"Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}")
        spend_summary = f"""
        <div>
          <ul>
            <li>Type: Rekey</li>
            <li>From: {from_root}</li>
            <li>To: {new_root}</li>
            <li>Slow factor: {timelock}</li>
            <li>Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}</li>
          </ul>
        </div>
        """
        if validate_against is not None:
            derivation = load_root_derivation(validate_against)
            re_derivation = calculate_puzzle_root(
                derivation.prefarm_info,
                derivation.pubkey_list,
                derivation.required_pubkeys,
                derivation.maximum_pubkeys,
                derivation.minimum_pubkeys,
            )
            if re_derivation.prefarm_info.puzzle_root == new_root and re_derivation == derivation:
                print(f"Configuration successfully validated against root: {new_root}")
            elif re_derivation == derivation:
                expected: bytes32 = new_root
                got: bytes32 = re_derivation.prefarm_info.puzzle_root
                print(f"Configuration does not validate. Expected {expected}, got {got}.")
            else:
                print("Configuration is malformed, could not validate")
    elif spend_type == "REKEY_CANCEL":
        spending_pubkey = get_spending_pubkey_for_drop_coin(solution)
        new_root, old_root, timelock = get_info_for_rekey_drop(puzzle)
        print("Type: Rekey Cancel")
        print(f"From: {old_root}")
        print(f"To: {new_root}")
        print(f"Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}")
        spend_summary = f"""
        <div>
          <ul>
            <li>Type: Rekey Cancel</li>
            <li>From: {old_root}</li>
            <li>To: {new_root}</li>
            <li>Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}</li>
          </ul>
        </div>
        """
    elif spend_type == "PAYMENT_CLAWBACK":
        spending_pubkey = get_spending_pubkey_for_drop_coin(solution)
        _, p2_ph = get_info_for_ach_drop(puzzle)
        print("Type: Payment Clawback")
        print(f"Amount: {spend.coin.amount}")
        print(f"To: {encode_puzzle_hash(p2_ph, 'xch')}")
        print(f"Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}")
        spend_summary = f"""
        <div>
          <ul>
            <li>Type: Payment Clawback</li>
            <li>Amount: {spend.coin.amount}</li>
            <li>To: {encode_puzzle_hash(p2_ph, 'xch')}</li>
            <li>Spenders: {BLSPublicKey(spending_pubkey).as_bech32m()}</li>
          </ul>
        </div>
        """
    else:
        print("Spend is not signable")
        return

    # Transform the bundle in qr codes and then inline them in a div
    all_qr_divs = ""
    normal_qr_width: Optional[float] = None
    for segment in chunks_for_zlib_blob(bytes(bundle), qr_density):
        qr_int = b2a_qrint(segment)
        qr = segno.make_qr(qr_int)
        if len(segment) == qr_density or normal_qr_width is None:
            normal_qr_width = qr.symbol_size()[0]
            scale: float = 3
        else:
            scale = 3 * (normal_qr_width / qr.symbol_size()[0])
        all_qr_divs += f"<div>{qr.svg_inline(scale=scale)}</div>"

    total_doc = f"""
    <html width='100%' height='100%'>
        <body width='100%' height='100%'>
            <div width='100%' height='100%'>
              {spend_summary}
              <div style='display:flex; flex-wrap: wrap; width:100%;'>
                {all_qr_divs}
              </div>
            </div>
        </body>
    </html>
    """

    # Write to a temporary file and open in a browser
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html")
    try:
        tmp_path = Path(tmp.name)
        tmp.write(bytes(total_doc, "utf-8"))
        tmp.close()
        webbrowser.open(f"file://{tmp_path}", new=2)
        input("Press Enter to exit")
    finally:
        os.unlink(tmp.name)


@click.command("which_pubkeys", short_help="Determine which pubkeys make up an aggregate pubkey")
@click.argument("aggregate_pubkey", nargs=1, required=True)
@click.option(
    "-pks",
    "--pubkeys",
    help="A comma separated list of pubkey files that may be in the aggregate",
    required=True,
)
@click.option(
    "-m",
    "--num-pubkeys",
    help="Check only combinations of a specific number of pubkeys",
    type=int,
    required=False,
)
@click.option(
    "--no-offset",
    help="Do not try the synthetic versions of the pubkeys",
    is_flag=True,
)
def which_pubkeys_cmd(
    aggregate_pubkey: str,
    pubkeys: str,
    num_pubkeys: Optional[int],
    no_offset: bool,
):
    agg_pk: G1Element = list(load_pubkeys(aggregate_pubkey))[0]
    pubkey_list: List[G1Element] = list(load_pubkeys(pubkeys))

    pubkey_file_dict: Dict[str, str] = {}
    for pk, file in zip(pubkey_list, pubkeys.split(",")):
        pubkey_file_dict[str(pk)] = file

    search_range = range(1, len(pubkey_list) + 1) if num_pubkeys is None else range(num_pubkeys, num_pubkeys + 1)
    for m in search_range:
        for subset in itertools.combinations(pubkey_list, m):
            aggregated_pubkey = G1Element()
            for pk in subset:
                aggregated_pubkey += pk
            if aggregated_pubkey == agg_pk or (
                not no_offset
                and PrivateKey.from_bytes(
                    calculate_synthetic_offset(aggregated_pubkey, DEFAULT_HIDDEN_PUZZLE_HASH).to_bytes(32, "big")
                ).get_g1()
                + aggregated_pubkey
                == agg_pk
            ):
                print("The following pubkeys match the specified aggregate:")
                for pk in subset:
                    print(f" - {pubkey_file_dict[str(pk)]}")
                return

    print("No combinations were found that matched the aggregate with the specified parameters.")
