# Backend hosting assessment

The frontend can stay on Vercel. The backend needs FFmpeg, Whisper, temporary
video files, and outbound TLS/TCP access to Tiger Data. A frontend deployment
does not provide that backend. The repository now selects paid Render compute;
changing this file does not itself create or deploy a hosting service.

## Render: selected backend deployment

The root `render.yaml` selects `1c-2g`: 1 CPU and 2 GB RAM, formerly called
Standard. [The current plan mapping](https://render.com/docs/compute-plans)
accepts both names. Its published compute rate is $25/month, prorated while
active. A purchased Pro **workspace** is a separate subscription and does not
upgrade a Free web service's RAM automatically. The cost comparisons below
exclude that already selected workspace fee.

1. In the intended Render workspace, create a Blueprint from
   `cepahal/HackNCQuantro`, branch `main`, using the root `render.yaml`. Review
   the paid compute before applying. If configuring a web service manually,
   choose Docker, `1c-2g` / Standard compute, and one instance.
2. Keep the repository root as the service root: the blueprint already sets
   Dockerfile Path `./backend/Dockerfile.vercel` and Docker Context `./backend`.
   Do not combine these repo-relative paths with another `/backend` root.
   [Render's Docker fields](https://render.com/docs/blueprint-spec) describe these
   paths. Leave Docker Command empty; the image uses `0.0.0.0` and Render's `PORT`.
3. Fill `sync: false` values privately from the existing verified configuration:
   both Tiger URLs, table prefix, Gemini key/model, Exa key, and applicable
   messaging values. Preserve Tiger TLS verification. Translate any certificate
   path embedded in a DSN to a real Linux container path. If the server requires
   the existing custom CA, provision that public CA and point both clients to
   its mounted path; the system CA path in the blueprint is sufficient only when
   it actually validates the Tiger server. Never upload credentials in source.
4. Set `FRONTEND_ORIGIN=https://quantro-ten.vercel.app` and
   `REPORT_BASE_URL=https://quantro-ten.vercel.app`. Preserve
   `TRANSCRIPTION_PROVIDER=whisper` and the image's baked
   `WHISPER_MODEL=/opt/quantro/whisper`; do not copy the laptop's `small.en`
   override or a Windows `MEDIA_TOOLS_DIR` path.
5. Keep health check `/openapi.json`. After deployment, verify its actual HTTPS
   service URL returns the analysis, status, report, and ingest routes. This
   proves startup, not Gemini, search, download, or full-analysis success. Use the
   URL assigned to this service; the already occupied `quantro-api.onrender.com`
   hostname was found to serve an unrelated application.
6. Set Vercel Production `NEXT_PUBLIC_API_BASE_URL` to that verified Render HTTPS
   origin. Remove a stale `NEXT_PUBLIC_REPORT_API_BASE_URL` or set it to the same
   origin. Redeploy Vercel so the public variables reach the browser bundle.
   Keep all provider/Tiger credentials exclusively in backend private settings.
7. From the deployed frontend, verify CORS preflight, a real analysis, status
   polling, report/display retrieval, and media playback. Observe Linux peak RAM
   and temporary disk use; 2 GB is an initial capacity choice, not a measured
   full-pipeline guarantee. Admit only one demo analysis at a time until a tested
   global concurrency gate exists.

The Docker image includes Node 24 and supervises the existing Photon Spectrum
bridge beside Python when `PHOTON_ENABLED=true`. Configure the existing private
`SPECTRUM_PROJECT_ID`, `SPECTRUM_PROJECT_SECRET`, and `PHOTON_BRIDGE_TOKEN` in
Render. The bridge stays on loopback; `/imessage` exposes the consent-based
registration page and `/imessage/qr.svg` links to that page. A QR scan opens
registration; submitting the form requests the welcome message. Keep one
listener instance and persist `PHOTON_DATA_DIR` to retain intake and delivery
receipts across deployments. A provider-accepted send still requires a handset
check to confirm receipt.

[Render documents long HTTP responses up to 100 minutes](https://render.com/docs/heroku),
whereas [Railway closes a request after five minutes without transferred data](https://docs.railway.com/networking/public-networking/specs-and-limits)
and permits up to 15 minutes with ongoing transfer. Browser/client timeouts and
the application's stale-job cutoff remain independent. After a dropped request,
recover using the existing status endpoint instead of automatically resubmitting.

## Railway: the current free service limits do not safely fit

[Official plan limits](https://docs.railway.com/pricing/plans) and
[pricing](https://railway.com/pricing) currently specify:

| Resource | Free trial | Ongoing Free |
| --- | --- | --- |
| Credit | $5 once, expires after 30 days | $1 each month, no rollover |
| Service RAM | 1 GB | 0.5 GB |
| Service CPU | Up to 2 vCPU | Up to 1 vCPU |
| Ephemeral disk | 1 GB | 1 GB |
| Persistent volume | 0.5 GB | 0.5 GB |
| Container image | 4 GB maximum | 4 GB maximum |

There is no 8 GB free service trial. The 8 GB per-replica figure belongs to the
paid Hobby plan. Extra credits do not increase the free service RAM limit.

An isolated macOS small-Whisper test recorded approximately 980 MiB, as documented
in `docs/PROVIDERS.md`; this is not a Linux whole-pipeline benchmark. Interpreting the
trial limit as 1 GiB leaves only about 44 MiB. Retrieval permits 100,000,000-byte
media files, and `tigerdata_storage.py` reads a complete file into memory before
uploading it. Subsequent candidate retrieval can run while Whisper remains
cached. The full supported flow therefore lacks safe trial memory headroom;
the ongoing 0.5 GB limit is smaller still. A smaller Whisper model is not a
verified fix: its peak memory and transcript/alignment quality need measurement.
The image size and temporary-disk peak also need verification.

The [trial verification rules](https://docs.railway.com/pricing/free-trial)
distinguish Full Trial with full network access from Limited Trial with restricted
outbound ports. Confirm the actual Tiger connection succeeds; the documentation
does not promise its custom PostgreSQL port is available on Limited Trial.

### Conditional usage cost

[Container resource prices](https://docs.railway.com/pricing/plans) are metered
usage: approximately $0.0139 per GB of RAM-hour, $0.0278 per vCPU-hour,
$0.05 per GB of network egress, and $0.000208 per GB of volume-hour.

`cost ≈ hours × (0.0139 × average_RAM_GB + 0.0278 × average_vCPU + 0.000208 × volume_GB) + 0.05 × egress_GB`

For example, 1 GB RAM plus an average of 1 vCPU costs about $0.0417/hour; $5
covers roughly 120 such hours before storage/egress. At 2 GB plus 1 vCPU it is
about $0.0556/hour, or 90 hours, but 2 GB is unavailable on the free service trial.
These are conditional calculations, not measurements or a promise of free uptime.
An idle running service can still incur memory usage. Hobby has a $5 monthly
minimum. [Compute hard limits](https://docs.railway.com/pricing/cost-control)
take workloads offline at the configured usage threshold; they do not waive
subscription charges.

### Existing-repository configuration if the resource gate is resolved

Configure this through the dashboard. Railway's current
[Config as Code documentation](https://docs.railway.com/config-as-code) says new
services cannot adopt `railway.json` / `railway.toml`; legacy support ends on
December 1, 2026. No obsolete config file or new IaC dependency is added here.

1. After choosing a plan with sufficient resources, create an empty backend
   service, then connect `cepahal/HackNCQuantro`, branch `main`. Configure its
   Root Directory as `/backend` before deploying. This makes Docker `COPY`
   instructions resolve against the existing backend requirements and source.
2. In service Variables set `RAILWAY_DOCKERFILE_PATH=Dockerfile.vercel`.
   Set Watch Paths to `/backend/**`. Leave the Start Command override empty:
   the Docker CMD already binds `0.0.0.0` and consumes Railway's injected `PORT`.
   See the official [Dockerfile](https://docs.railway.com/builds/dockerfiles) and
   [monorepo](https://docs.railway.com/guides/deploying-a-monorepo) instructions.
3. Use one replica, an initial 2 GB RAM limit, and
   [Healthcheck Path](https://docs.railway.com/deployments/healthchecks)
   `/openapi.json`. This checks startup; it does not prove provider readiness or
   serialize analyses. The single-analysis demo restriction below still applies.
4. Add existing real values privately to Railway Variables using the root
   `.env.example` as the name reference: `TIGER_DATABASE_URL`, `TIGERDATA_DSN`,
   their existing table-prefix/TLS settings, `SEARCH_PROVIDER`, its provider key,
   `GEMINI_API_KEY`, and `GEMINI_MODEL`. Set `APP_ENV=production` and
   `REPORT_STORE=tiger`. Preserve verified Tiger TLS settings and provision any
   referenced certificate file securely inside the container; a laptop file
   path will not exist there. Never upload the local env file to GitHub.
5. Set backend `FRONTEND_ORIGIN=https://quantro-ten.vercel.app` and
   `REPORT_BASE_URL=https://quantro-ten.vercel.app`. Do not copy the local
   `WHISPER_MODEL=small.en` override: the Docker image already sets
   `WHISPER_MODEL=/opt/quantro/whisper` to its baked model. Do not copy a Windows
   `MEDIA_TOOLS_DIR`; FFmpeg and ffprobe are installed on the image's PATH.
6. Deploy, then use Settings / Networking / Generate Domain. Test the actual
   generated HTTPS origin's `/openapi.json`; confirm it contains
   `/analyze/{job_id}/run`, `/analyze/{job_id}/status`, `/reports/{report_id}`, and
   `/ingest/{job_id}`. Confirm a real Tiger connection and startup checks pass.
7. In the Vercel project's Production environment set `NEXT_PUBLIC_API_BASE_URL`
   to that verified HTTPS backend origin. Set `NEXT_PUBLIC_REPORT_API_BASE_URL`
   to the same origin or remove a stale temporary-gateway override. Redeploy
   Vercel: public Next.js variables are baked into the frontend build. Provider
   keys and Tiger credentials belong only in backend private variables.
8. From `https://quantro-ten.vercel.app`, verify the analysis POST, status polling,
   report, display, and media requests use the new origin. Verify the CORS
   preflight permits that exact frontend origin and a real report renders.
   Also test downloads from this cloud IP; local success does not establish it.

## Paid Railway versus paid Render

Paid Railway Hobby removes the trial's main resource caps. The
[current pricing page](https://railway.com/pricing) allows up to 8 GB RAM and
8 vCPU per replica. The [plan resource table](https://docs.railway.com/pricing/plans)
lists 100 GB ephemeral disk, 100 GB container-image size, and up to 5 GB persistent
volume storage for Hobby. These are limits, not resources included for a flat $5.
The actual built image size has not been measured; an assumed 8 GB image is not
evidence of the current image's size.

Railway's [billing rules](https://docs.railway.com/pricing/understanding-your-bill)
make the monthly charge `max($5, resource usage)` before taxes, rather than
`$5 + all resource usage`. Assume a full $5 minimum even for a short demo;
[the current-period plan fee is not prorated on account deletion](https://docs.railway.com/pricing/faqs).
Stop deployments when finished and cancel before the next billing cycle to avoid
another minimum charge. Existing other services consume the same included credit.

Render's [Standard / 1c-2g instance](https://render.com/docs/compute-plans) has
1 CPU and 2 GB RAM at [the published $25/month compute rate](https://render.com/pricing).
Its [Hobby workspace has no monthly plan fee, and compute is prorated by the second](https://render.com/docs/faq).
This is distinct from Render's paid Pro workspace subscription; that subscription
is not needed for this comparison. Current Hobby
[outbound bandwidth](https://render.com/docs/outbound-bandwidth) includes 5 GB
per month, then costs $0.15/GB. Older comparisons mentioning 100 GB are outdated.

### Conditional cost comparison

Assume one new paid backend service, no persistent volume, 2 GB total outbound
traffic, no additional billed build usage, and no other workspace services.
The monthly example uses the published 30-day resource rates; the 48-hour demo
starts and ends within one billing cycle, then its compute is stopped. RAM and
CPU columns are average actual Railway usage, not the configured memory ceiling.
Taxes and the existing Tiger/provider API costs are additional and excluded from
both sides equally.

| Average RAM / CPU | Railway, 30 days | Render 2 GB, 30 days | Railway, 48 hours then stop | Render 2 GB, 48 hours then stop |
| --- | --- | --- | --- | --- |
| 1 GB / 0.1 vCPU | $12.10 | $25.00 | $5.00 minimum | About $1.67 |
| 1 GB / 0.5 vCPU | $20.10 | $25.00 | $5.00 minimum | About $1.67 |
| 2 GB / 0.1 vCPU | $22.10 | $25.00 | $5.00 minimum | About $1.67 |
| 2 GB / 0.5 vCPU | $30.10 | $25.00 | $5.00 minimum | About $1.67 |

For example, Railway's first row is `$10 × 1 + $20 × 0.1 + $0.05 × 2`.
Render's demo compute is approximately `$25 × 48 / 720`; exact proration depends
on the billing month's length and active seconds. The 2 GB egress example fits
Render's monthly allowance. Running into a second Railway billing cycle can incur
another minimum; leaving either service running continues its applicable charges.

For a backend that exists only for a 48-hour demo, Render's prorated 2 GB instance
is cheaper under these assumptions. Railway Hobby is cheaper for a continuing
month with approximately 1 GB resident RAM and low average CPU, and makes a later
RAM increase easy. At 2 GB resident RAM, its compute crosses Render's $25 rate
near an average 0.25 vCPU, before egress. These are workload-dependent estimates,
not measured deployed bills.

### Project fit and acceptance gate

Start with a 2 GB RAM limit, one replica, and one Uvicorn worker on either paid
host. Verify a complete analysis at the permitted media size before relying on
that memory limit. One worker alone does not serialize FastAPI's synchronous
routes: multiple requests can run in threads. The initial demo must admit only
one analysis at a time until a tested global concurrency limit is in place.
Per-job idempotency does not limit different jobs.

Keep the existing Docker image, baked Whisper model, Tiger persistence, and private
provider variables. No extra Railway database or volume is necessary for the
existing Tiger persistence contract. Temporary files still require monitoring and
cleanup. Prove startup, Tiger TLS, analysis, report/display/media retrieval, and
the actual Vercel CORS origin before calling the deploy ready. More RAM does not
guarantee that YouTube, Instagram, or TikTok permits downloads from a cloud IP;
each supported platform needs a live test from the selected host.

## Cloud Run: best compatible managed free allowance

Cloud Run can reuse `backend/Dockerfile.vercel` and supports
[outbound TCP to public internet destinations](https://docs.cloud.google.com/run/docs/securing/security).
An initial configuration is 1 vCPU, 2 GiB RAM, concurrency 1, minimum instances
0, maximum instances 1, request-based billing, and a 900-second request timeout.
Measure peak RAM before calling this sufficient: Cloud Run's
[writable filesystem consumes instance RAM and is ephemeral](https://docs.cloud.google.com/run/docs/container-contract).

The [request-based monthly free allowance](https://cloud.google.com/run/pricing)
is 180,000 vCPU-seconds, 360,000 GiB-seconds, and two million requests, aggregated
per billing account. Internet egress has a separate 1 GiB North America allowance;
builds, image storage, and other services can have separate charges.

The user must enable a Google Cloud billing account before deployment.
An eligible [Free Trial](https://docs.cloud.google.com/free/docs/free-cloud-features)
provides $300 for 90 days without automatic charges unless upgraded. A paid
billing account can incur overage charges beyond allowances. This is a free
allowance, not guaranteed permanently free hosting. Existing Gemini API access
does not establish Cloud Run billing readiness, and trial credit does not fund
Gemini API usage in AI Studio.

After a backend passes startup and a real analysis, set Vercel's
`NEXT_PUBLIC_API_BASE_URL` to its actual HTTPS origin and redeploy the frontend.
Set backend `FRONTEND_ORIGIN` and `REPORT_BASE_URL` to the Vercel origin.
Verify Tiger TLS, a full analysis, and large-media playback on the chosen host.

## Other free options

- [Hugging Face Spaces](https://huggingface.co/docs/hub/spaces-overview) has
  16 GB RAM on CPU Basic with no hourly hardware charge, but new Docker/Gradio
  Spaces require a paid account plan. Outbound ports are limited to 80, 443, and
  8080, so the current direct Tiger connection cannot work unchanged.
- [AWS Free Plan](https://aws.amazon.com/free/free-tier-faqs/) offers eligible new
  accounts credits for up to six months. It is temporary credit, not indefinite
  free hosting; account eligibility and instance capacity must be checked.
- [Oracle Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
  currently lists 2 ARM OCPUs and 12 GB RAM for free tenancies. It has enough
  memory in principle, but availability, ARM dependencies, HTTPS setup, and idle
  reclamation make it a separate deployment task rather than an immediate guarantee.
- [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
  can temporarily expose an already running local service without an account.
  They provide no compute or uptime guarantee, require the laptop to remain
  running, and change hostname on restart. The existing read-only report gateway
  does not accept analysis submissions; exposing the full API requires separate
  access and long-request testing.


## Current production connection (2026-10-10)

- Frontend: https://quantro-ten.vercel.app
- API: https://quantro-backend.onrender.com
- Vercel Production: `NEXT_PUBLIC_API_BASE_URL` and
  `NEXT_PUBLIC_REPORT_API_BASE_URL` both use the API origin above.
- Render: `FRONTEND_ORIGIN` and `REPORT_BASE_URL` use the frontend origin above.
  Tiger/provider settings and the Tiger CA are configured privately.
- Render uses the approved 1 CPU / 2 GB service. Deploy the tested commit
  manually; automatic deployment is currently disabled to avoid untested pushes.
- The Vercel project is linked locally to the approved CLI account. From the
  repository root, run `vercel deploy --prod --yes --scope bhrigu-s-projects1`;
  this includes only the frontend via `.vercelignore`. The project is not yet
  Git-connected. Never upload local environment files.

The public MP4 form/report/media flow passed. Fresh YouTube retrieval is blocked
by cloud-IP bot verification, so the homepage uses a labeled saved report whose
limitations disclose reused transcripts. This connection does not host the
separate Node Photon listener. See `temporary-fixes.md` for remaining demo gates.
