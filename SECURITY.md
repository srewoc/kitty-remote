# Security policy

Please report vulnerabilities privately through GitHub Security Advisories. Do not open a public issue containing passwords, device credentials, session cookies, relay addresses, terminal content, or screenshots.

Kitty Remote intentionally lets a browser type into terminals on your desktop. Anyone who can sign in to the relay can act with the same authority as the paired terminal windows. Use a strong admin password, serve the relay only over HTTPS, pair only computers you control, and revoke devices you no longer use.

The relay is trusted: terminal content passes through it after TLS termination, although it is never written to disk. Run it on a server you control. Device credentials are stored as digests on the relay and in `0600` files on the desktop; never commit `.state/`, `deploy/.env`, credential files, or test artifacts.

Security updates target the latest `main` branch.
