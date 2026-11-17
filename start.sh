#!/bin/bash
set -euo pipefail

# NetBird keeps its peer identity (WireGuard key, management login) here.
mkdir -p /app/data/netbird /run/netbird /run/router
chmod 700 /app/data/netbird /run/netbird
chown cloudron:cloudron /run/router

# The status page must listen on the container's Cloudron address only, never on
# the NetBird interface, so mesh peers cannot reach it through the tunnel.
STATUS_BIND="$(ip -4 -o addr show dev eth0 2>/dev/null | awk '{ split($4, a, "/"); print a[1]; exit }' || true)"
if [[ -z "${STATUS_BIND}" ]]; then
    echo "start: no IPv4 address on eth0; the status page and health check need the container's Cloudron address" >&2
    exit 1
fi
export STATUS_BIND

exec /usr/bin/supervisord --configuration /app/code/supervisord.conf --nodaemon
