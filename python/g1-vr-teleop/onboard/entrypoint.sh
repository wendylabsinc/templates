#!/bin/sh
# Start the onboard commander with the template's settings.
set -eu

net="${G1_NET:-auto}"
if [ "$net" = "auto" ]; then
  # The interface that carries the G1's internal 192.168.123.0/24 network.
  net="$(ip -o -4 addr show | awk '$4 ~ /^192\.168\.123\./ { print $2; exit }')"
  if [ -z "$net" ]; then
    echo "[onboard] no interface has a 192.168.123.x address."
    echo "[onboard] Is this running on the G1's computer? Set NETWORK_INTERFACE to override."
    exit 1
  fi
  echo "[onboard] robot network interface: $net"
fi

set -- python3 g1_onboard.py --net "$net" --domain "$G1_DOMAIN" --port "$G1_PORT" --key "$G1_KEY"

# A dead-man is required on the real robot (domain 0). "none" means a second
# person holds the Unitree remote and uses its damping combo as the stop.
case "${G1_DEADMAN:-}" in
  none) set -- "$@" --no-deadman ;;
  "") ;;
  *) set -- "$@" --deadman "$G1_DEADMAN" ;;
esac

case "${G1_LOCOMOTION:-off}" in
  on) set -- "$@" --locomotion ;;
esac

exec "$@"
