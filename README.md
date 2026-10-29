Internal Custody
=======

## Install
You need a running full node and wallet for most commands to work.  You don't need anything if you are just a signer. Here are the instructions for this piece:
```
py -m venv venv
. ./venv/bin/activate  # ./venv/Scripts/activate for Windows users
pip install --extra-index-url https://pypi.chia.net/simple/ chia-internal-custody
```

If you're on Windows, you need one extra package:

`pip install pyreadline`

Supported Python: 3.11-3.14 (matches chia-blockchain main).

## Key Generation
```
hsmgen > 1.se
hsmgen > 2.se
hsmgen > 3.se
hsmpk $(cat 1.se) > 1.pk
hsmpk $(cat 2.se) > 2.pk
hsmpk $(cat 3.se) > 3.pk
```
_Note: If you're on windows powershell 5 or lower, `>` does not work properly._
_Either upgrade your shell or find some other way to put the output into a file as UTF-8._

## Configuration
```
cic init --withdrawal-timelock 60 --payment-clawback 120 --rekey-cancel 120 --rekey-timelock 120 --slow-penalty 120
cic derive_root -pks "1.pk,2.pk,3.pk" -m 2 -n 3
```

## Launch
```
cic launch_singleton --fee 100000000
cic sync -c './Configuration (<your 6 hex digits>).txt'
cic p2_address --prefix txch
<send money to that address and wait for confirmation>
cic sync
```


## Payment
```
cic payment -f initial_absorb.unsigned -pks "1.pk,2.pk" -a 100 -t <own address> -ap -at 0
cat ./initial_absorb.unsigned | hsms -y --nochunks 1.se
echo <sig here> > initial_absorb.1.sig
cat ./initial_absorb.unsigned | hsms -y --nochunks 2.se
echo <sig here> > initial_absorb.2.sig
hsmmerge ./initial_absorb.unsigned ./initial_absorb.1.sig ./initial_absorb.2.sig > initial_absorb.signed
cic push_tx -b ./initial_absorb.signed -m 100000000
cic sync
```

## Generating a series of payments

Generate the files in push order. Each file after the first is built against the singleton as it will be after the earlier files confirm:
```
cic payment ... -f p1
cic payment ... --previous-spends p1 -f p2
cic payment ... --previous-spends p1,p2 -f p3
```
The files can be unsigned spend files or signed spend files. List them in push order.

Each spend must be pushed no sooner than `withdrawal_timelock` seconds after the previous spend confirms. The singleton changes with every payment, so a series can never be pushed as one bundle. Any other spend of the singleton in between leaves the remaining files unusable, and they must be regenerated with `cic payment --previous-spends`.

To verify a list against the synced state, run:
```
cic check_series --db-path <db> --spends p1,p2,p3
```
This command is read-only. It prints the earliest push time and reports any file that does not follow the series.

Store signed spend files with the same care as other signed transactions.

## Clawback
````
cic clawback -f clawback.unsigned -pks "1.pk,2.pk"
cat ./clawback.unsigned | hsms -y --nochunks 1.se
echo <sig here> > clawback.1.sig
cat ./clawback.unsigned | hsms -y --nochunks 2.se
echo <sig here> > clawback.2.sig
hsmmerge ./clawback.unsigned ./clawback.1.sig ./clawback.2.sig > clawback.signed
cic push_tx -b ./clawback.signed -m 100000000
cic sync
````

## Re-configure
```
cic derive_root --db-path './sync (<your hex digits>).sqlite' -c './Configuration (new).txt' -pks "1.pk,2.pk" -m 1 -n 2
```

## Rekey
```
cic start_rekey -f rekey.unsigned -pks "1.pk,2.pk" -new './Configuration (new).txt'
cat ./rekey.unsigned | hsms -y --nochunks 1.se
echo <sig here> > rekey.1.sig
cat ./rekey.unsigned | hsms -y --nochunks 2.se
echo <sig here> > rekey.2.sig
hsmmerge ./rekey.unsigned ./rekey.1.sig ./rekey.2.sig > rekey.signed
cic push_tx -b ./rekey.signed -m 100000000
cic sync
```

## Complete
```
cic complete -f complete.signed
cic push_tx -b ./complete.signed -m 100000000
cic sync
```

## Update DB config
```
cic update_config -c './Configuration (new).txt'
```

## Increase security
Check the maximum lock level first:
```
cic show -d --db-path './sync (<your hex digits>).sqlite'
```
Start a rekey to the next lock level with exactly the current number of required keys:
```
cic increase_security_level -f lock.unsigned -pks "1.pk,2.pk"
cat ./lock.unsigned | hsms -y --nochunks 1.se
echo <sig here> > lock.1.sig
cat ./lock.unsigned | hsms -y --nochunks 2.se
echo <sig here> > lock.2.sig
hsmmerge ./lock.unsigned ./lock.1.sig ./lock.2.sig > lock.signed
cic push_tx -b ./lock.signed -m 100000000
```
The rekey can be pushed once the singleton has not been spent for rekey_increments. It can be cancelled with `cic clawback` until it is completed. After rekey_clawback_period, sync and complete it:
```
cic sync
cic complete -f complete.signed
cic push_tx -b ./complete.signed -m 100000000
cic sync
```
`cic sync` updates the configuration automatically.

## Fees
`cic push_tx --fee` attaches a fee transaction to the push. A bundle that already pays a fee is refused when it is pushed with `--fee N` where N > 0: pushing it that way would pay a second fee. Push the fee-bearing hex without `--fee` (or with `--fee 0`), or use the fee-less hex.
