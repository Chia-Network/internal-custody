"""Frozen option and argument surface of the CLI commands."""

from typing import Any, Dict, Tuple

UNSET = "<unset>"

BASELINE_PARAMS: Dict[str, Tuple[Tuple[Any, ...], ...]] = {
    "audit": (
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("filepath", ("--filepath", "-f"), (), UNSET, False, False, False, "text", 1),
        ("diff", ("--diff", "-d"), (), UNSET, False, False, False, "text", 1),
    ),
    "clawback": (
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("filename", ("--filename", "-f"), (), None, False, False, False, "text", 1),
        ("pubkeys", ("--pubkeys", "-pks"), (), UNSET, True, False, False, "text", 1),
    ),
    "complete": (
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("filename", ("--filename", "-f"), (), None, False, False, False, "text", 1),
    ),
    "derive_root": (
        (
            "configuration",
            ("--configuration", "-c"),
            (),
            "./Configuration (needs derivation).txt",
            False,
            False,
            False,
            "text",
            1,
        ),
        ("db_path", ("--db-path", "-db"), (), None, False, False, False, "text", 1),
        ("pubkeys", ("--pubkeys", "-pks"), (), UNSET, True, False, False, "text", 1),
        ("initial_lock_level", ("--initial-lock-level", "-m"), (), UNSET, True, False, False, "text", 1),
        ("maximum_lock_level", ("--maximum-lock-level", "-n"), (), UNSET, False, False, False, "text", 1),
        ("minimum_pks", ("--minimum-pks", "-min"), (), 1, False, False, False, "integer", 1),
        ("validate_against", ("--validate-against", "-va"), (), None, False, False, False, "text", 1),
    ),
    "examine_spend": (
        ("spend_file", ("spend_file",), (), UNSET, True, False, False, "text", 1),
        ("qr_density", ("--qr-density",), (), 250, False, False, False, "integer", 1),
        ("validate_against", ("--validate-against", "-va"), (), None, False, False, False, "text", 1),
    ),
    "export_config": (
        ("filename", ("--filename", "-f"), (), None, False, False, False, "text", 1),
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("public", ("--public", "-p"), (), UNSET, False, True, False, "boolean", 1),
    ),
    "increase_security_level": (
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("pubkeys", ("--pubkeys", "-pks"), (), UNSET, True, False, False, "text", 1),
        ("filename", ("--filename", "-f"), (), None, False, False, False, "text", 1),
    ),
    "init": (
        ("directory", ("--directory", "-d"), (), ".", False, False, False, "text", 1),
        ("withdrawal_timelock", ("--withdrawal-timelock", "-wt"), (), UNSET, True, False, False, "text", 1),
        ("payment_clawback", ("--payment-clawback", "-pc"), (), UNSET, True, False, False, "text", 1),
        ("rekey_cancel", ("--rekey-cancel", "-rc"), (), UNSET, True, False, False, "text", 1),
        ("rekey_timelock", ("--rekey-timelock", "-rt"), (), UNSET, True, False, False, "text", 1),
        ("slow_penalty", ("--slow-penalty", "-sp"), (), UNSET, True, False, False, "text", 1),
    ),
    "launch_singleton": (
        (
            "configuration",
            ("--configuration", "-c"),
            (),
            "./Configuration (awaiting launch).txt",
            True,
            False,
            False,
            "text",
            1,
        ),
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("wallet_rpc_port", ("--wallet-rpc-port", "-wp"), (), None, False, False, False, "integer", 1),
        ("fingerprint", ("--fingerprint", "-f"), (), None, False, False, False, "integer", 1),
        ("node_rpc_port", ("--node-rpc-port", "-np"), (), None, False, False, False, "integer", 1),
        ("fee", ("--fee",), (), 0, False, False, False, "integer", 1),
    ),
    "p2_address": (
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("prefix", ("--prefix", "-p"), (), "xch", False, False, False, "text", 1),
    ),
    "payment": (
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("filename", ("--filename", "-f"), (), None, False, False, False, "text", 1),
        ("pubkeys", ("--pubkeys", "-pks"), (), UNSET, True, False, False, "text", 1),
        ("amount", ("--amount", "-a"), (), 0, False, False, False, "integer", 1),
        ("recipient_address", ("--recipient-address", "-t"), (), UNSET, True, False, False, "text", 1),
        (
            "absorb_available_payments",
            ("--absorb-available-payments", "-ap"),
            (),
            UNSET,
            False,
            True,
            False,
            "boolean",
            1,
        ),
        ("maximum_extra_cost", ("--maximum-extra-cost", "-mc"), (), 50, False, False, False, "integer", 1),
        ("amount_threshold", ("--amount-threshold", "-at"), (), 1000000000000, False, False, False, "integer", 1),
    ),
    "push_tx": (
        ("spend_bundle", ("--spend-bundle", "-b"), (), UNSET, True, False, False, "text", 1),
        ("wallet_rpc_port", ("--wallet-rpc-port", "-wp"), (), None, False, False, False, "integer", 1),
        ("fingerprint", ("--fingerprint", "-f"), (), None, False, False, False, "integer", 1),
        ("node_rpc_port", ("--node-rpc-port", "-np"), (), None, False, False, False, "integer", 1),
        ("fee", ("--fee", "-m"), (), 0, False, False, False, "integer", 1),
        ("confirmation_timeout", ("--confirmation-timeout",), (), 900, False, False, False, "float", 1),
        ("poll_interval", ("--poll-interval",), (), 10, False, False, False, "float", 1),
    ),
    "show": (
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("config", ("--config", "-c"), (), UNSET, False, True, False, "boolean", 1),
        ("derivation", ("--derivation", "-d"), (), UNSET, False, True, False, "boolean", 1),
    ),
    "start_rekey": (
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("filename", ("--filename", "-f"), (), None, False, False, False, "text", 1),
        ("pubkeys", ("--pubkeys", "-pks"), (), UNSET, True, False, False, "text", 1),
        ("new_configuration", ("--new-configuration", "-new"), (), UNSET, True, False, False, "text", 1),
    ),
    "sync": (
        ("configuration", ("--configuration", "-c"), (), None, False, False, False, "text", 1),
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
        ("node_rpc_port", ("--node-rpc-port", "-np"), (), None, False, False, False, "integer", 1),
        ("show", ("--show", "-s"), (), UNSET, False, True, False, "boolean", 1),
    ),
    "update_config": (
        ("configuration", ("--configuration", "-c"), (), None, False, False, False, "text", 1),
        ("db_path", ("--db-path", "-db"), (), "./", True, False, False, "text", 1),
    ),
    "which_pubkeys": (
        ("aggregate_pubkey", ("aggregate_pubkey",), (), UNSET, True, False, False, "text", 1),
        ("pubkeys", ("--pubkeys", "-pks"), (), UNSET, True, False, False, "text", 1),
        ("num_pubkeys", ("--num-pubkeys", "-m"), (), UNSET, False, False, False, "integer", 1),
        ("no_offset", ("--no-offset",), (), UNSET, False, True, False, "boolean", 1),
    ),
}
