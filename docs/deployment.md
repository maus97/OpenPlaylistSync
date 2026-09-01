# Deploying Open Playlist Sync

OPS is designed to run as one private application instance. The supplied
Compose configuration binds to loopback by default and separates the database
and locally generated security keys into different named volumes. Do not expose
the application port directly to the public internet.

## Before deployment

1. Choose the address where you will open OPS, for example
   `https://ops.example.net` on a private network or behind a VPN.
2. Configure HTTPS in a reverse proxy before making the service available
   outside the host computer. OPS has its own local administrator password,
   but HTTPS is still required to protect that password in transit.
3. Add the matching Spotify redirect address in the Spotify Developer Dashboard:
   `https://ops.example.net/auth/spotify/callback`.
4. Set `OPS_ENVIRONMENT=production`, `OPS_SESSION_COOKIE_SECURE=true`, and
   `OPS_ALLOWED_HOSTS=ops.example.net` when OPS is served through HTTPS.
5. Set `OPS_TRUSTED_PROXY_IPS` only to the exact address or CIDR of a reverse
   proxy you operate. Do not trust arbitrary forwarded-client headers.
6. Keep the container, database volume, secret volume, and backups private.
   Anyone who gets both data and encryption keys may be able to recover provider
   credentials.

## Docker Compose

1. Install Docker Engine or Docker Desktop on the host.
2. Download this repository and open the directory containing `compose.yaml`.
3. Start OPS:

   ```sh
   docker compose up -d --build
   ```

4. Retrieve the high-entropy one-time setup code from the container console:

   ```sh
   docker compose exec ops python -m ops.security.bootstrap
   ```

5. Open `http://127.0.0.1:8000` on the Docker host, or use the HTTPS address
   configured in your reverse proxy. Enter the setup code, create the local
   administrator password, and store that password in a password manager. The
   setup code is consumed after successful setup.
6. Open **Settings** and follow the Spotify and YouTube Music guides linked from
   each provider title. Provider passwords are entered only on provider pages.

The named `ops-data` volume contains `/data/ops.db`. The named `ops-secrets`
volume contains session, encryption, and temporary bootstrap material. Protect
and back up both, but do not combine or expose them unnecessarily. When upgrading
an older deployment, OPS securely copies legacy key files from `/data` into the
secret volume on first start; retain the old volume until the upgrade is verified.

## Reverse proxy

Use an HTTPS reverse proxy such as Caddy, Nginx Proxy Manager, or Traefik.
Proxy requests to OPS, restrict access to trusted users, and forward the
original host and HTTPS scheme. Keep the direct application port loopback-only
or place it on a private container network. OPS provides a local administrator
password with memory-hard scrypt hashing, signed expiring sessions, atomic
source-based throttling, and bounded verification concurrency. A proxy or VPN
access policy is still recommended as a network boundary.

After the proxy is working, update Spotify's redirect address to the external
HTTPS URL and reconnect Spotify from the OPS interface. Google device-code
authorization does not use a callback address, but YouTube Data API v3 must be
enabled in the selected Google Cloud project.

## Backup and restore

Back up the data and secret volumes while OPS is stopped so SQLite and its keys
remain consistent. Resolve the exact volume names with `docker volume ls` before
running backup commands, then store the two archives with restricted access.

```sh
docker compose stop ops
docker run --rm -v openplaylistsync_ops-data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/ops-data-backup.tgz -C /data .
docker run --rm -v openplaylistsync_ops-secrets:/secrets:ro -v "$PWD":/backup alpine \
  tar czf /backup/ops-secrets-backup.tgz -C /secrets .
docker compose start ops
```

The volume names may differ by Compose project. The data archive contains
encrypted provider credentials; the secret archive contains the key needed to
decrypt them. Store them separately where practical.

To restore, stop OPS, restore each archive into its corresponding new or empty
volume, then start OPS. The container runs Alembic migrations automatically.
Confirm the administrator login, accounts, pairs, and latest run history before
applying a new sync.

## Updates and rollback

1. Create a volume backup.
2. Run `docker compose pull` if using a published image, or obtain the updated
   source checkout.
3. Run `docker compose up -d --build`.
4. Check `/healthz`, Settings, and the latest activity entry.
5. If the update fails, stop OPS, restore the volume backup, return to the
   previous image/source version, and start the service again.

Migration `0009_security_remediation_state` changes synchronization approval,
lease, identity, and mapping records. Roll back the application and database
together; do not run an older image against a database left at revision `0009`.

Run only one OPS container against a SQLite data volume. Multiple replicas can
produce concurrent writes and are not supported.
