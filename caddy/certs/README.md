# Tailscale certs

Not git-tracked (private key lives here). Regenerate with:

    sudo tailscale cert minisserver.tail6bf4d5.ts.net

then move the .crt/.key here and restart Caddy. See the renew timer at
/srv/compose/_system/tailscale-cert-renew/ for the automated version.
