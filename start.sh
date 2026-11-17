#!/bin/bash
set -euo pipefail

# NetBird keeps its peer identity (WireGuard key, management login) here.
mkdir -p /app/data/netbird /run/netbird /run/router
chmod 700 /app/data/netbird /run/netbird
chown cloudron:cloudron /run/router

# The status page must listen on the container's Cloudron address only, never on
# the NetBird interface, so mesh peers cannot reach it through the tunnel.
STATUS_BIND="$(ip -4 -o addr show dev eth0 | awk '{ split($4, a, "/"); print a[1]; exit }')"
export STATUS_BIND="${STATUS_BIND:-127.0.0.1}"

exec /usr/bin/supervisord --configuration /app/code/supervisord.conf --nodaemon
