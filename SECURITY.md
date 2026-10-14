# Security policy

## Supported versions

Only the latest release receives security fixes. Please upgrade before reporting.

## Reporting a vulnerability

Please do **not** open a public issue. Report it privately through GitHub:
[Report a vulnerability](https://github.com/magicstack-llp/zim-pro/security/advisories/new).

Include what an attacker can do, the steps to reproduce it, and the version affected. You
will get an answer within a week. Once a fix is released, the advisory is published, with
credit to you unless you prefer otherwise.

## What to know when running Zim Pro

- **Set `KIWIX_ADMIN_TOKEN`** on any server others can reach. Without it, anyone who can
  open the web interface can change settings, delete books and use your AI providers.
- **Lock `/mcp`** with a key (Settings → MCP) before exposing it.
- **The Docker socket** (needed only for browser capture with Zimit) gives the container
  control of Docker on the host, which is as good as root. Leave it out unless you need it.
- Zim Pro has no TLS of its own. Put it behind a reverse proxy with HTTPS before exposing it
  to the internet.
- API keys and VPN passwords are stored in the database in `/state`. Keep that folder
  private, and out of backups you share.
