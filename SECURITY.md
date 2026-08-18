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
When landscape poster maintenance is enabled, it can also replace matching
`*-poster.*` image files with `*-background.*` images and create
`*-poster-backup.*` files in mounted media folders.
Duplicate cleanup can move, delete, or rename confirmed files under the mounted
library root and records applied cleanup actions under `/state`.

### State Backups

`POST /system/backup` streams the whole `/state` directory as a zip and, like
every other endpoint, is unauthenticated. Anyone who can reach the port can
download it, so the archive is built on the assumption that it is readable by
any host on the network.

Stored credentials are therefore blanked before they enter the archive. Any
JSON file under `/state` containing an `emby_api_key` is reserialized with that
value emptied; the backup manifest (`vid2gif-backup.json`) lists which files
were redacted under `redacted`. Restoring a backup restores everything except
those credentials, which must be re-entered on the Settings page. Non-JSON
files and JSON files with no credentials are archived byte-for-byte.

This limits the blast radius of the unauthenticated endpoint; it is not a
substitute for keeping the app off the public internet. The archive still
contains library paths, file names, maintenance logs, and job history.

### Excluded From Backups

The Emby library index under `/state/emby-index` is left out of the archive
entirely and listed under `excluded` in the manifest. It records what has been
watched and marked as a favourite, which is a different order of personal than
maintenance logs, and there is no field to blank the way the Emby key is blanked
-- the whole file is the sensitive part. Restoring a backup therefore does not
restore the index; rebuild it from the Settings page instead.

### Credentials at Rest

Redaction applies to the backup archive only. The Emby API key is stored in
plain text in `/state/app_settings.json`, because the app has to present it to
Emby on every request and holds no secret with which to encrypt it. Anyone who
can read the `/state` volume -- another container sharing the mount, a host
user, a filesystem backup -- can read the key. Treat `/state` as sensitive, and
prefer an Emby key scoped to the smallest set of permissions vid2gif needs.

## Reporting Issues

For security issues, open a private report through GitHub's security advisory
workflow if available. If this repository is mirrored elsewhere, contact the
repository owner directly before public disclosure.
