# Security Policy

## Intended Use

vid2gif is intended for trusted private networks only. It does not include
authentication, CSRF protection, rate limiting, or public-internet hardening.

Do not expose the app directly to the public internet. If remote access is
required, place it behind a private VPN, firewall, or authenticated reverse
proxy.

## Data Exposure Model

Users who can access the Web UI may be able to see mounted library paths, video
file names, job status, output paths, and job logs. Only mount directories that
the container should be allowed to inspect and write to.

The app writes generated GIFs as `poster.gif` next to selected source videos.

## Reporting Issues

For security issues, open a private report through GitHub's security advisory
workflow if available. If this repository is mirrored elsewhere, contact the
repository owner directly before public disclosure.
