# NetBird Router for Cloudron

A NetBird routing peer packaged as a Cloudron app. It makes Cloudron apps marked
**VPN access only** reachable from a [NetBird](https://netbird.io) mesh, the way
Cloudron's OpenVPN app does for OpenVPN clients.

> **Status: experimental.** Cloudron 10.1.6 only accepts the OpenVPN app as the
> VPN app. This package needs the change proposed in the
> [Cloudron forum](https://forum.cloudron.io/topic/16295), which lets the admin
> select any app with the `net_admin` capability. Until Cloudron ships it, this
> package only runs on a test Cloudron with that patch applied.

## How it works

1. Cloudron trusts traffic from the selected VPN app's container IP on apps
   marked VPN access only, and denies everyone else.
2. This app runs a NetBird client in that container with `net_admin`. It joins
   your NetBird account as a **masquerading routing peer**, so traffic from mesh
   peers reaches Cloudron from the container's own IP.
3. Every 30 seconds it reads the protected apps from Cloudron's internal
   `GET $CLOUDRON_APP_BRIDGE_ORIGIN/vpn-access-config` endpoint (only the
   selected VPN app gets them). It then keeps one NetBird network in step:
   - this peer as the network's router, with masquerade on
   - one **domain resource** per protected FQDN (primary, secondary and alias)
   - in Cloudron's *custom* tunnel mode, one resource per custom CIDR
4. All resources go into the group **Cloudron protected apps**. You decide who
   can use them with a normal NetBird access policy.

It only changes resources it created; their description says
`managed by cloudron-netbird-router`. It never creates or changes access
policies, and it never deletes anything when Cloudron stops selecting it.

Cloudron's *full* tunnel mode isn't automated. If you want mesh peers to send
all their traffic through Cloudron, set up an exit node in NetBird yourself.

## Setup

You need a NetBird management server (for example the
[NetBird Cloudron app](https://github.com/marcusquinn/cloudron-netbird-app)).

1. In NetBird, create a **service user** for the router with the *Network admin*
   role and a personal access token. Use the longest expiry NetBird allows.
2. Create a **one-off setup key** for the router peer.
3. Install this app, then configure it:

   ```bash
   cloudron env set --app router.example.com \
     NETBIRD_MANAGEMENT_URL=https://netbird.example.com:33073 \
     NETBIRD_API_URL=https://netbird.example.com \
     NETBIRD_SETUP_KEY=... \
     NETBIRD_API_TOKEN=...
   ```

   `NETBIRD_MANAGEMENT_URL` is what clients use with `netbird up`;
   `NETBIRD_API_URL` is the dashboard origin. The setup key is only used for
   the first login, so you can delete it afterwards.
4. In Cloudron, select this app as the VPN app (**Network > VPN Access**), then
   mark apps as VPN access only.
5. In NetBird, create an access policy from the groups that should reach the
   protected apps to the destination group **Cloudron protected apps**.

The app's own page shows its sync status. It's behind Cloudron login.

Optional settings: `NETBIRD_NETWORK_NAME` (default `Cloudron <dashboard domain>`),
`NETBIRD_RESOURCE_GROUP`, `NETBIRD_HOSTNAME` and `SYNC_INTERVAL` (seconds,
minimum 10).

## Security notes

- **Cloudron trusts everything that leaves this container.** Don't add other
  software to it, and don't select the NetBird management server app as the
  VPN app: it runs public services.
- **Access is all-or-nothing per Cloudron host.** Every protected FQDN resolves
  to the same host IP and port 443, and NetBird policies match destination IPs.
  So a peer allowed to reach one protected app can request the others. Each
  app's own login remains a separate boundary.
- The router runs with `--disable-client-routes`, so routes from other NetBird
  routers can't pull its own traffic away from Cloudron.
- The status page listens on the container's Cloudron address only, not on the
  NetBird interface.

## Development

```bash
cloudron build
cloudron install --location router
cloudron logs -f --app router
```

## License

MIT. NetBird is a trademark of NetBird GmbH; this package is not affiliated
with NetBird or Cloudron.
