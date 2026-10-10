# Phone-accessible report demo

Keep the backend on loopback port 8000 and the Next.js frontend on loopback port
3000. Do not tunnel the backend. The read-only gateway forwards only allowlisted
report pages, sanitized report/display JSON, linked media, and fixed static assets.
It denies analysis, ingest, raw job data, docs, mutations, and unlisted reports.

1. Obtain `cloudflared` from Cloudflare's official download page and verify its
   SHA256 against the official release asset digest. Keep the portable binary
   under ignored `.data/tools/`.
2. Start a temporary tunnel to `http://127.0.0.1:3100` with its normal QUIC
   transport. If Windows certificate validation requires a PEM bundle, combine
   existing trusted ROOT public certificates with the exact three Cloudflare
   roots embedded in the verified release and supply `--cacert`. A custom pool
   replaces the embedded pool; Windows roots alone are insufficient. Never
   disable certificate verification. Use official connectivity prechecks to
   identify whether the supported transport is available.
3. Privately set root `.env.local` `REPORT_BASE_URL` to the printed HTTPS origin
   and `PHOTON_PUBLIC_REPORTS_FILE=.data/photon/public-reports.json`. Set root
   `FRONTEND_ORIGIN=http://localhost:3000` so local browser submissions remain
   allowed independently of the shared report origin. Set
   `frontend/.env.local` `NEXT_PUBLIC_REPORT_API_BASE_URL=/api` for same-origin
   public reads. Keep `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` for local web
   analysis submissions and local report reads; the public gateway accepts shared
   report reads only. Loopback pages use the local API even with sharing enabled.
4. Build and start the frontend, then start `node frontend/scripts/report-gateway.mjs`.
   Restart the single Photon listener after changing its environment. Restart the
   backend only after active jobs finish, so it loads the local CORS origin and
   public report URL.
5. The listener allowlists each saved report before attempting its reply. To share
   an already saved report, explicitly add its actual ID to the ignored JSON array.
   Anyone with a shared URL can read that report and its linked media.

```powershell
npm.cmd --prefix frontend run build
npm.cmd --prefix frontend start
node frontend/scripts/report-gateway.mjs
# Separate terminal, using the actual official executable and trusted PEM paths:
.data/tools/cloudflared.exe tunnel --cacert .data/tools/cloudflared-trusted-roots.pem --url http://127.0.0.1:3100 --protocol quic
npm.cmd --prefix backend/services/photon run start:imessage
```

Verify public report JSON/display and media range requests; confirm public
`/docs`, `/api/ingest/...`, `/api/analyze/...`, and unlisted reports are denied.
Tap the HTTPS link from a real phone. A local browser check alone is not the
mobile acceptance test.

Rollback: stop the tunnel and gateway with Ctrl+C, clear the private sharing
allowlist, restore root `REPORT_BASE_URL=http://localhost:3000`, keep
`FRONTEND_ORIGIN=http://localhost:3000`, unset
`PHOTON_PUBLIC_REPORTS_FILE`, and unset the private frontend
`NEXT_PUBLIC_REPORT_API_BASE_URL`. Keep `NEXT_PUBLIC_API_BASE_URL` at
`http://localhost:8000`. Rebuild/restart the frontend and restart the listener.
Stopping the temporary tunnel invalidates its URLs; restart creates a new origin.

The 60-second report target is not guaranteed. Source downloads, CPU transcription,
and bounded Gemini retries remain real work; validation errors are never converted
to successful findings or to `no_verified_match`.

Certificate behavior is documented in Cloudflare's [pinned TLS implementation](https://github.com/cloudflare/cloudflared/blob/2026.10.0/tlsconfig/origin_ca.go)
and [embedded certificate bundle](https://github.com/cloudflare/cloudflared/blob/2026.10.0/tlsconfig/cloudflare_ca.go).
See the [official connectivity prechecks](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/troubleshoot-tunnels/connectivity-prechecks/) for transport diagnostics.
