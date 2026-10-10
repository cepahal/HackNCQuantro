# Backend hosting assessment

The frontend can stay on Vercel. The backend needs FFmpeg, Whisper, temporary
video files, and outbound TLS/TCP access to Tiger Data. A frontend deployment
does not provide that backend. No hosting service has been created or purchased
as part of this assessment.

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

Local small-Whisper testing reached approximately 980 MiB. Even interpreting the
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

Use one backend service from `main`, root directory `/backend`,
`RAILWAY_DOCKERFILE_PATH=Dockerfile.vercel`, and watch path `/backend/**`.
The existing Dockerfile binds `0.0.0.0` and consumes Railway's `PORT`; no start
command override is needed. Use `/openapi.json` for the health check and one
replica. Keep Tiger and provider values in private service variables. Preserve
the image's baked Whisper model path. See the official
[Dockerfile](https://docs.railway.com/builds/dockerfiles) and
[monorepo](https://docs.railway.com/guides/deploying-a-monorepo) instructions.

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

- [Hugging Face Spaces](https://huggingface.co/docs/hub/spaces-overview) provides
  16 GB RAM on free CPU hardware, but outbound ports are limited to 80, 443, and
  8080. The current direct Tiger PostgreSQL connection cannot work unchanged.
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
