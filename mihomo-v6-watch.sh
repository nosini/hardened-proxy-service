#!/bin/bash
# Re-run mihomo-v6-direct.sh whenever addresses or routes change. Changes arrive in bursts
# (a router advertisement adds addresses and routes together, mihomo adds its own routes
# on start), so wait until events stop for a second before re-running, but for at most
# DEBOUNCE seconds in all. A failed run is retried every RETRY seconds until one succeeds,
# even if no further events arrive.
# Arguments are passed on to mihomo-v6-direct.sh.
DIRECT=/usr/local/sbin/mihomo-v6-direct.sh
RETRY=10
DEBOUNCE=5
QUIET=1

ip monitor address route | {
  ok=0
  "$DIRECT" "$@" && ok=1
  while :; do
    if [ "$ok" = 1 ]; then
      read -r _ || break
    else
      read -r -t "$RETRY" _
      # 1 is the end of the monitor's output; above 128 is the timeout
      [ "$?" != 1 ] || break
    fi
    # Wait for a second of quiet, but never more than DEBOUNCE seconds from the first
    # event: a steady stream of events must not postpone the re-run forever.
    deadline=$((SECONDS + DEBOUNCE))
    while [ "$SECONDS" -lt "$deadline" ] && read -r -t "$QUIET" _; do :; done
    ok=0
    "$DIRECT" "$@" && ok=1
  done
}
