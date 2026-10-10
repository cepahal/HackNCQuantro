# Quantro benchmark pilot

Nine variants of three public NASA assets. These are not nine independent recordings.
Two assets share Kennedy speech content. Famous English archive clips do not represent general social media.
References are NASA published transcripts, not manually adjudicated audio: differences below are provisional, not gold WER.
Single runs include cold model loading. No universal winner or timestamp-accuracy claim is justified.

| Case | Provider | Status | Reference difference | Seconds |
|---|---|---|---:|---:|
| choose_clean | gemini | ok | 2.8% | 3.082 |
| choose_clean | whisper | ok | 2.8% | 2.966 |
| choose_compressed | gemini | ok | 2.8% | 3.11 |
| choose_compressed | whisper | ok | 2.8% | 2.437 |
| choose_noise | gemini | ok | 2.8% | 2.813 |
| choose_noise | whisper | ok | 2.8% | 3.528 |
| commit_clean | gemini | ok | 1.9% | 2.479 |
| commit_clean | whisper | ok | 1.9% | 2.102 |
| commit_compressed | gemini | ok | 1.9% | 2.922 |
| commit_compressed | whisper | ok | 1.9% | 2.091 |
| commit_noise | gemini | ok | 1.9% | 2.523 |
| commit_noise | whisper | ok | 1.9% | 2.095 |
| smallstep_clean | gemini | ok | 23.1% | 4.479 |
| smallstep_clean | whisper | ok | 16.9% | 5.57 |
| smallstep_compressed | gemini | ok | 24.6% | 5.268 |
| smallstep_compressed | whisper | ok | 26.2% | 5.266 |
| smallstep_noise | gemini | ok | 23.1% | 4.284 |
| smallstep_noise | whisper | ok | 20.0% | 5.792 |

## Search

Missing keys/errors are not successful empty results. Exact target URL hits are a lower bound:
equivalent recordings require blind human review. Articles discussing the speech are not recording matches.

- exa: 0/9 completed; no accuracy score available.
- serpapi: 0/9 completed; no accuracy score available.

## Remaining evaluation

- Add at least 10 independent recordings across speakers, languages and recording conditions.
- Human-check reference words and timing before selecting an accuracy winner.
- Review candidate recordings, download accessibility, and alignment against known timestamps.
- Keep source groups separate between development and held-out evaluation.
- Re-run paired trials for latency; check provider billing for actual cost.
- Test no-source and unrelated-source cases before claiming reliable discovery.

## Pilot decision

Both providers produced identical normalized words on the six Kennedy cases.
The three Apollo cases differ; the published reference omits disfluencies and
contains an unclear span, so a smaller difference rate is not necessarily more
accurate speech recognition. Keep the existing local Whisper configuration for
now because it needs no transcription API key and supplies word timing already
used by alignment. This is an operational choice, not an accuracy winner.

Live search was blocked by missing EXA_API_KEY and SERPAPI_API_KEY.
Mock adapter tests passed, but their live API access and result quality remain
unverified. No search calls or paid subscriptions were made for this pilot.
Gemini used the existing account; exact billing cost is unknown.

Validation: 629 tests passed, 1 skipped, and 45 subtests passed.
