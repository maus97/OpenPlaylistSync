# Deploying Open Playlist Sync

OPS is designed to run as one private application instance. It stores its
database and locally generated security keys together in the persistent data
volume. Do not expose it directly to the public internet.

## Before deployment

1. Choose the address where you will open OPS, for example
   `https://ops.example.net` on a private network or behind a VPN.
2. Configure HTTPS in a reverse proxy before making the service available
   outside the host computer. OPS has its own local administrator password,
   but HTTPS is still required to protect that password in transit.
3. Add the matching Spotify redirect address in the Spotify Developer Dashboard:
   `https://ops.example.net/auth/spotify/callback`.
4. Set `OPS_SESSION_COOKIE_SECURE=true` when OPS is served through HTTPS.
5. Keep the container, database volume, and backups private. Anyone who gets
   both the database and its generated keys may be able to recover provider
   credentials.

## Docker Compose

1. Install Docker Engine or Docker Desktop on the host.
2. Download this repository and open the directory containing `compose.yaml`.
3. Start OPS:

   ```sh
   docker compose up -d --build
   ```

4. Open `http://SERVER-IP:8000` from your trusted network, or use the HTTPS
   address configured in your reverse proxy.
5. On the first visit, create and confirm the local administrator password.
   OPS will require it for later browser sessions. Then open **Settings** and
   follow the Spotify and YouTube Music guides linked from each provider title.
   Do not put normal operator credentials in a shell command.

The named `ops-data` volume contains `/data/ops.db`, `.ops-credential-key`, and
`.ops-session-secret`. Keep all three together.

## Reverse proxy

Use an HTTPS reverse proxy such as Caddy, Nginx Proxy Manager, or Traefik.
Proxy requests to `http://ops:8000` on the Docker network, restrict access to
trusted users, and forward the original host and HTTPS scheme. OPS provides a
local administrator password, with scrypt password hashing, signed sessions,
five failed attempts before a 15-minute account lockout, and an additional
per-client rate limit. A proxy or VPN access policy is still recommended as a
network boundary; do not expose OPS directly to the public internet.

After the proxy is working, update Spotify's redirect address to the external
HTTPS URL and reconnect Spotify from the OPS interface. Google device-code
authorization does not use a callback address, but YouTube Data API v3 must be
enabled in the selected Google Cloud project.

## Backup and restore

Back up the entire Docker volume while OPS is stopped so SQLite and both secret
files remain consistent:

```sh
docker compose stop ops
docker run --rm -v openplaylistsync_ops-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/ops-data-backup.tgz -C /data .
docker compose start ops
```

The volume name may differ; use `docker volume ls` to find the one created by
your Compose project. Store the archive securely because it contains encrypted
credentials and the keys that protect them.

To restore, stop OPS, restore the archive into a new or empty OPS data volume,
then start OPS. The container runs Alembic migrations automatically. Confirm the
accounts, pairs, and latest run history before applying a new sync.

## Updates and rollback

1. Create a volume backup.
2. Run `docker compose pull` if using a published image, or obtain the updated
   source checkout.
3. Run `docker compose up -d --build`.
4. Check `/healthz`, Settings, and the latest activity entry.
5. If the update fails, stop OPS, restore the volume backup, return to the
   previous image/source version, and start the service again.

Run only one OPS container against a SQLite data volume. Multiple replicas can
produce concurrent writes and are not supported.
