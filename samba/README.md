# Samba (host package, not containerized)

Not a compose stack — see host/MANIFEST.md for why. The live config is
/etc/samba/smb.conf, mirrored (read-only copy) at host/etc/samba/smb.conf.

This directory just holds the Samba user credentials, same .env
convention as every other stack, for anyone rotating the password later:

    sudo smbpasswd -a smbshare   # then update .env

Share: \192.168.31.2\library (read-only, SMB3 minimum, LAN interface
only - localhost/loopback is deliberately excluded, test from another
host or use the LAN IP).
