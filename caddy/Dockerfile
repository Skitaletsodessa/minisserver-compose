# Task 10: adds the caddy-ratelimit module so Immich's login endpoint behind Tailscale Funnel
# can be rate-limited per real client IP. Standard official pattern (xcaddy via the caddy
# -builder image); nothing else changed from the stock image. Pin both stages together.
#
# Why a custom build, not fail2ban/iptables: Funnel traffic arrives over the Tailscale tunnel,
# not as a raw connection to the host's network interface, so OS-level firewall banning of the
# real client IP has nothing to act on at the point traffic reaches this host — Caddy itself is
# the only place in the chain that both sees the real client IP (via X-Forwarded-For, once
# trusted_proxies is set) and can refuse a request. See docs/report-10.md.
FROM caddy:2.11.4-builder AS builder
RUN xcaddy build --with github.com/mholt/caddy-ratelimit@5625512f24f6f59d6f64fb3aafe5eecff0b286db

FROM caddy:2.11.4
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
