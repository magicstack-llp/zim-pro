# Docker samples

Ready-to-run `docker compose` files for Zim Pro. Pick the one that matches what you want;
each is complete on its own.

| File | What you get | Needs in `.env` |
|---|---|---|
| [`compose.yml`](compose.yml) | Zim Pro on its own | nothing |
| [`compose.solver.yml`](compose.solver.yml) | + captcha solver (FlareSolverr) | nothing |
| [`compose.vpn.yml`](compose.vpn.yml) | + VPN (Gluetun) for links and feeds, with the solver inside it | VPN details |
| [`compose.vpn-external.yml`](compose.vpn-external.yml) | Uses a Gluetun you already run elsewhere | `VPN_NETWORK` |
| [`compose.vpn-all.yml`](compose.vpn-all.yml) | All of Zim Pro's traffic through the VPN | VPN details |
| [`compose.zimit.yml`](compose.zimit.yml) | + browser capture (Zimit), with the Docker socket | `DOCKER_GID` |
| [`compose.redis.yml`](compose.redis.yml) | + Redis for the queues and a stream of the logs | nothing |
| [`compose.full.yml`](compose.full.yml) | VPN, solver, browser capture and Redis together | VPN details, `DOCKER_GID` |

## Use one

```sh
cd docker
cp .env.example .env             # then edit it; the plain compose.yml works without changes
mkdir -p zims state              # or point ZIM_DIR and STATE_DIR in .env at your own folders
docker compose -f compose.vpn.yml up -d
```

Then open http://localhost:8083. To avoid typing `-f` every time, copy the file you chose
over `compose.yml`, or add `COMPOSE_FILE=compose.vpn.yml` to `.env`.

The VPN and the solver do nothing until their addresses are saved in **Settings → Network**.
Each file lists the addresses to enter at its top; **Test connection** there checks them.

## Which VPN file?

- **`compose.vpn.yml`**: the usual choice. Only the pages fetched for links and feeds go
  through the VPN. Nothing needs adding to the Zim Pro service: containers in one file
  reach each other by name.
- **`compose.vpn-external.yml`**: you already run Gluetun for other things. Zim Pro joins
  its network instead of starting a second VPN.
- **`compose.vpn-all.yml`**: the catalog, ZIM downloads and Ask AI go through the VPN too.
  The web interface is unreachable while the VPN container is down.

## Notes

- A VPN file refuses to start until the values it needs are in `.env`, and says which one
  is missing.
- Gluetun needs `/dev/net/tun` on the host. If it is missing: `sudo modprobe tun`.
- The Docker socket (`compose.zimit.yml`, `compose.full.yml`) gives the container control
  of Docker on the machine. Leave it out unless you need browser capture.
- The `compose.yml` at the top of the repository is different: it builds the image from
  source, for development.
