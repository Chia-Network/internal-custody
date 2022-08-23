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
