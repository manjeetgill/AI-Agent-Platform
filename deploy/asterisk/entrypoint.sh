#!/bin/sh
set -eu
umask 077
password=$(cat /run/secrets/sip_password)
case "$password" in *[!A-Za-z0-9]*|'') echo 'Invalid SIP secret format' >&2; exit 1;; esac
[ "${#password}" -ge 32 ] || { echo 'SIP secret too short' >&2; exit 1; }
mkdir -p /tmp/asterisk /tmp/ast-run /tmp/ast-log /tmp/ast-data /tmp/ast-spool
cp /opt/asterisk/* /tmp/asterisk/
# Only direct peers on the private voice network are local. Mac port-forwarded
# packets arrive through the other network and must receive loopback SDP.
bridge_ip=$(getent hosts voice-bridge | awk '/^[0-9]+[.]/ {print $1; exit}')
[ -n "$bridge_ip" ] || { echo 'Private audio bridge DNS unavailable' >&2; exit 1; }
voice_dev=$(ip -4 route get "$bridge_ip" | awk '{for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}')
voice_subnet=$(ip -4 route show dev "$voice_dev" scope link | awk '$1 != "default" {print $1; exit}')
case "$voice_subnet" in ''|*[!0-9./]*) echo 'Invalid private voice subnet' >&2; exit 1;; esac
ip -o -4 addr show scope global | awk '{split($4,a,"/"); print "local_net=" a[1] "/32"}' > /tmp/asterisk/local-addresses
sed -i '/VOICE_SUBNET/r /tmp/asterisk/local-addresses' /tmp/asterisk/pjsip.conf
sed -i "s|VOICE_SUBNET|$voice_subnet|" /tmp/asterisk/pjsip.conf
printf '\n[pilot-auth]\ntype=auth\nauth_type=userpass\nusername=1001\npassword=%s\n' "$password" >> /tmp/asterisk/pjsip.conf
unset password
exec asterisk -f -C /tmp/asterisk/asterisk.conf
