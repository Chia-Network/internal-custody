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
```
cic increase_security_level -f lock.unsigned -pks "1.pk,2.pk"
cat ./lock.unsigned | hsms -y --nochunks 1.se
echo <sig here> > lock.1.sig
cat ./lock.unsigned | hsms -y --nochunks 2.se
echo <sig here> > lock.2.sig
hsmmerge ./lock.unsigned ./lock.1.sig ./lock.2.sig > lock.signed
cic push_tx -b ./lock.signed -m 100000000
cic sync
```

## Two-step push for lock level increases

A lock level increase creates the lock coin and the new singleton coin inside the same bundle. Chia 2.7.5 rejects relative time conditions on coins created and spent in the same bundle, so `cic push_tx` automatically pushes such a bundle in two steps. Step 1 contains the START_REKEY spend. Once the lock coin and the new singleton coin have at least one confirmation, step 2 spends them. Step 2 normally needs no signature, so anyone can submit it. Use `--confirmation-timeout` and `--poll-interval` to control how long the CLI waits for step 1 confirmations.

### Fees on both steps

`cic push_tx --fee N` attaches a fee transaction of N mojos to each step, so the total fee is 2 * N mojos. For a lock level increase the only coin announcement comes from the lock coin completion in step 2. Step 2 binds its fee to that coin announcement. Step 1 binds its fee with `ASSERT_CONCURRENT_SPEND` on its first spend. The wallet fee coins of the two steps are disjoint: the second fee excludes the coins of the first, so the two steps cannot double-spend each other. Both fee transactions are built before anything is pushed. If the bundle has no coin announcement to link a fee to, the CLI says so and pushes nothing. Use `--fee 0` in that case.

### Retrying step 2

If step 2 does not land, the lock coin stays pending on chain and the CLI prints the exact step 2 bundle hex. Push it later with `cic push_tx -b <hex>` (or save the hex to a file and pass the path). The printed step 2 hex can be re-pushed as is.

When a fee was attached, the CLI also prints a fee-less step 2 hex. A step 2 hex that already contains a fee must not be pushed with `--fee`, because that attaches a second fee. Either push the fee-bearing hex without `--fee`, or push the printed fee-less hex with `--fee N` to attach a new fee.

### Pending lock coins

A pending lock coin (created by step 1, 0 mojos, timelock 0) cannot be cancelled, because `rekey_clawback` requires a timelock greater than 0, and it never expires. Anyone can complete it at any later time, even after the singleton has returned to the old root by an unrelated rekey. Completing it only raises the lock level and needs one more signer than before, so there is no fund risk. This can surprise signers if the singleton returned to the old root. The signers can neutralise a stale pending lock by rekeying the singleton to a new root. `cic complete` can rebuild step 2 for a pending lock coin, for example if the printed hex went stale. After a timeout, verify with `cic sync` (or a block explorer) that the lock coin and the new singleton coin exist before pushing step 2. A step 2 pushed before they exist will be rejected.
