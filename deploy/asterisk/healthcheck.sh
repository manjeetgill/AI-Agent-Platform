#!/bin/sh
set -eu
for module in chan_pjsip.so res_pjsip_nat.so app_audiosocket.so app_echo.so func_uuid.so; do
    asterisk -C /tmp/asterisk/asterisk.conf -rx "module show like $module" | grep -q 'Running' || exit 1
done
