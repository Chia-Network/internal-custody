Internal Custody
=======

## Install
You need a running full node and wallet for these commands to work.  Here are the instructions for this piece:
```
git clone https://github.com/Chia-Network/internal-custody.git
cd ./internal-custody
py -m venv venv
. ./venv/bin/activate  # ./venv/Scripts/activate for Windows users
pip install .
pip install git+https://github.com/Quexington/hsms.git@main#3443d1941190b88a6e0fb37b8c32ab1b83f96c22
```

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
cic init --date <go get the unix time> --rate 100000000 --amount 1000000000000 --withdrawal-timelock 60 --payment-clawback 120 --rekey-cancel 120
cic derive_root -pks "1.pk,2.pk,3.pk" -m 2 -n 3 -rt 60 -sp 120
```

## Launch
```
cic launch_singleton --fee 100000000
cic sync -c './Configuration (<your 6 hex digits>).txt'
cic p2_address --prefix txch
<send money to that address and wait for confirmation>
cic sync
```

(You should wait 10 minutes at this point for the singleton to age a bit and make sure some assumptions that the code makes holds)

## Payment
```
cic payment -f initial_absorb.unsigned -pks "1.pk,2.pk" -a 1000000 -t <own address> -ap -at 0
cat ./initial_absorb.unsigned | hsms0 1.se
echo <sig here> > initial_absorb.1.sig
cat ./initial_absorb.unsigned | hsms0 2.se
echo <sig here> > initial_absorb.2.sig
hsmmerge ./initial_absorb.unsigned ./initial_absorb.1.sig ./initial_absorb.2.sig > initial_absorb.signed
cic push_tx -b ./initial_absorb.signed -m 100000000
cic sync
```

## Clawback
````
cic clawback -f clawback.unsigned -pks "1.pk,2.pk"
cat ./clawback.unsigned | hsms0 1.se
echo <sig here> > clawback.1.sig
cat ./clawback.unsigned | hsms0 2.se
echo <sig here> > clawback.2.sig
hsmmerge ./clawback.unsigned ./clawback.1.sig ./clawback.2.sig > clawback.signed
cic push_tx -b ./clawback.signed -m 100000000
cic sync
````

## Re-configure
```
cic derive_root --db-path './sync (<your hex digits>).sqlite' -c './Configuration (new).txt' -pks "1.pk,2.pk" -m 1 -n 2 -rt 30 -sp 60
```

## Rekey
```
cic start_rekey -f rekey.unsigned -pks "1.pk,2.pk" -new './Configuration (new).txt'
cat ./rekey.unsigned | hsms0 1.se
echo <sig here> > rekey.1.sig
cat ./rekey.unsigned | hsms0 2.se
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
cic increase_security_level -f lock.unsigned -pks "1.pk"
cat ./lock.unsigned | hsms0 1.se
echo <sig here> > lock.1.sig
hsmmerge ./lock.unsigned ./lock.1.sig > lock.signed
cic push_tx -b ./lock.signed -m 100000000
cic sync
```

## Update root after lock (WIP)
```
cic derive_root --db-path './sync (<your hex digits>).sqlite' -c './Post Lock Config.txt' -pks "1.pk,2.pk" -m 2 -n 2 -rt 30 -sp 60
cic update_config -c './Post Lock Config.txt'
```
