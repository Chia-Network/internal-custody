# Upgrading `cic` (internal-custody) to current main: operator runbook

This runbook moves an operator machine from an older `cic` install, including an install built from the `--previous-spends` (#69) branch, to current main. Existing configuration files, pubkey files, sync databases, on-chain singletons and pending spend files keep working. No on-chain migration is needed.

Replace `<TAG>` with the release tag or commit you are installing.

## Summary of changes

- **Payment series:** `payment --previous-spends` and the read-only `check_series` command (see section 5).
- **Lock-level increase:** `increase_security_level` is signed with the current number of required keys, not one more, and is completed with `cic complete` (see section 6).
- **RPC ports:** an explicit `--node-rpc-port` or `--wallet-rpc-port` is now honoured.
- **QR output:** commands that print QR codes without `-f` work with the current `hsms` release.
- **Python:** 3.11 to 3.14 are supported; 3.10 is not.

## 0. Before you start

- **Chia node and wallet:** chia-blockchain 2.7.5 or newer, synced, and a wallet reachable over RPC (the wallet is only needed for fees).
- **Target:** the release tag or commit `<TAG>`.
- **Python:** 3.11 or newer (3.11 to 3.14 are supported).
- **Rekeys (recommended, not required):** run `cic show -db <db>` on the current install and note whether `REKEYS:` is empty. `show` lists a pending lock-level increase the same way as a normal rekey, so if you started an `increase_security_level` with the current install and it is still under `REKEYS:`, it is simplest to complete or cancel it first. Other pending rekeys and payments carry over. No migration is needed and the new version can continue a pending rekey.
- **Payments and series:** pending payments and a pending payment series are fine; see section 5.
- **Timing:** allow time for the read-only checks in section 3 before switching.

## 1. Record the current state (current install)

```bash
cic sync -db <db>
cic show -db <db> -c -d > before_show.txt
cic p2_address -db <db> > before_p2.txt
```

If a payment series is in progress, write down which files have been pushed and confirmed and which are still pending, in push order.

## 2. Back up

Copy the following to a safe location:

- the sync database(s) (`sync (*).sqlite`)
- the configuration files (`Configuration (*).txt`)
- the pubkey files
- all unsigned and signed spend files that are not yet confirmed
- `before_show.txt` and `before_p2.txt`

Keep the old virtual environment untouched for rollback.

## 3. Install side by side and verify on copies (read-only)

```bash
python3.11 -m venv ~/cic-new && . ~/cic-new/bin/activate   # any 3.11 to 3.14 interpreter
python --version                                          # must be 3.11 to 3.14
pip install "git+https://github.com/Chia-Network/internal-custody@<TAG>"
cic --version
mkdir -p ~/cic-check && cp <db file> <configuration file> ~/cic-check/
cic show -db ~/cic-check/<db file> -c -d > after_show.txt
diff before_show.txt after_show.txt     # only the `Current time` line, and `(Ready at: ...)`/`(Ready)` on pending items, may differ
cic p2_address -db ~/cic-check/<db file> | diff before_p2.txt -
cic sync -db ~/cic-check/<db file>      # must succeed
cic examine_spend <an existing unsigned spend file>   # must render the summary and QR codes
```

The `examine_spend` step ends with a `Press Enter to exit` prompt.

**Signer check (no signature needed):**

1. Create one unsigned spend with the new install against the copy, for example `cic payment -db ~/cic-check/<db file> -pks <pubkeys> -t <address> -a <amount> -f check.unsigned`. `<amount>` is in mojos and must be an even number.
2. Have one signer open it with their usual `hsms` tool and confirm the details are shown correctly.
3. Stop at the confirmation prompt and do not sign. Delete `check.unsigned` afterwards.

If any check fails, stop and keep using the old install (section 7).

## 4. Switch

Use the new environment (`. ~/cic-new/bin/activate`) with the real database path from now on. The day-to-day commands are unchanged:

- `sync`
- `show`
- `p2_address`
- `payment`
- `examine_spend`
- `push_tx`
- `clawback`
- `complete`
- `audit`
- `export_config`

## 5. A payment series in progress

1. `cic sync -db <db>`.
2. Check the remaining files, in push order. Unsigned and signed files are both accepted:

   ```bash
   cic check_series -db <db> -s <next file>,<file after that>,...
   ```

   - **All OK:** continue as before. Push each file with `cic push_tx -b <file> [-m <fee>]`, no sooner than the withdrawal timelock after the previous payment confirms. Run `cic sync -db <db>` between pushes.
   - **A file is flagged:** that file and every file after it must be regenerated and signed again. Generate them with:

     ```bash
     cic payment -db <db> -pks <pubkeys> -t <address> -a <amount> --previous-spends <earlier pending files, in push order> -f <new file>.unsigned
     ```

     Delete the superseded signed files.
3. To add more payments to the series:

   ```bash
   cic payment -db <db> -pks <pubkeys> -t <address> -a <amount> --previous-spends <all pending files of the series, in push order> -f <next>.unsigned
   ```

   - List only files that are **not yet confirmed**. A confirmed or out-of-order file is rejected with an error naming it.
   - Files produced by the older `--previous-spends` branch are accepted.
4. While a series is pending, build every new payment of that singleton with `--previous-spends` listing the pending files. If any other spend of the singleton is confirmed first, run `check_series` again and regenerate the files it flags.
5. Keep signed spend files private until they are pushed.

## 6. Behaviour changes to know

- **`payment`:** new `-ps/--previous-spends` (comma separated, unsigned or signed files, in push order) and a new read-only command `check_series -db <db> -s <files>`.
- **QR output:** `payment`, `clawback`, `start_rekey` and `increase_security_level` without `-f`, and `examine_spend`, print and render QR codes as before.
- **`push_tx -b <file> [-m <fee>]`:**
  - Every spend is pushed as a single bundle, and `-m/--fee` works as before.
  - It refuses to add a second fee to a bundle that already contains one.
  - The options `--confirmation-timeout` and `--poll-interval` are removed. They were on main after 0.2.10 but never in a tagged release; remove them from any scripts.
- **`increase_security_level`:** this is now a standard rekey to the next lock level. Existing singletons and configurations need no migration for it.
  1. Check `cic show -db <db> -d` (`max lock level`) first.
  2. Sign it with the **current** number of required keys: for a 3-of-5 setup it is signed by 3 keys. Passing a different number fails with a message naming the expected number.
  3. Push it, then after the rekey period run `cic complete -db <db>` and push the result. `cic clawback -db <db> -pks <pubkeys>` cancels it until then.
  4. `cic sync -db <db>` updates the configuration automatically; `update_config` is not needed.
- **`--node-rpc-port` / `--wallet-rpc-port` (`-np`, `-wp`):** an explicit value now works. Before, it was ignored and the command failed.
- **Python:** 3.11 to 3.14 are supported; 3.10 is not.

## 7. Rollback

- Stop using the new environment and activate the old one. Its files are untouched.
- The sync database schema is unchanged, so the old version can open it. If in doubt, restore the backup from section 2 and run `cic sync -db <db>`.
- Signed spend files from the new version can be pushed with the old version. If you roll back before signing, regenerate any unsigned files with the old version.
