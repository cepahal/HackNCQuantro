"""Bounded, resumable evaluation of transcription and source discovery."""
import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from backend.services.processing import preprocessing
from backend.services.processing.preprocessing import extract_media_features
from backend.services.specialist.gemini import load_settings
from backend.services.external_apis.build_search_queries import build_search_queries

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / '.data/benchmark'
KEYS = {'exa': 'EXA_API_KEY', 'serpapi': 'SERPAPI_API_KEY'}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


def canonical(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username:
        raise ValueError('Invalid public result URL')
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.startswith('utm_') and k not in ('fbclid', 'gclid')]
    return urlunsplit(('https', parts.netloc.lower(), parts.path.rstrip('/'), urlencode(sorted(query)), ''))


def tokens(text):
    # Normalize punctuation/case, not semantic differences or contractions.
    return re.findall(r"\w+(?:'\w+)?", text.lower().replace('’', "'"))


def word_error(reference, hypothesis):
    ref, hyp = tokens(reference), tokens(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, a in enumerate(ref, 1):
        following = [i]
        for j, b in enumerate(hyp, 1):
            following.append(min(row[j] + 1, following[-1] + 1, row[j - 1] + (a != b)))
        row = following
    return {'reference_words': len(ref), 'edit_distance': row[-1],
            'wer': row[-1] / len(ref) if ref else None}


def fuse(pages, limit=5):
    """Round-robin ranks across identical query budgets; no provider score mixing."""
    results, seen = [], set()
    for rank in range(max((len(p) for p in pages), default=0)):
        for page in pages:
            if rank >= len(page):
                continue
            item = page[rank]
            identity = canonical(item['url'])
            if identity not in seen:
                seen.add(identity)
                results.append(item)
            if len(results) == limit:
                return results
    return results


def target_rank(results, targets):
    accepted = {canonical(u) for u in targets}
    return next((i for i, r in enumerate(results, 1) if canonical(r['url']) in accepted), None)


def command(args):
    subprocess.run(args, check=True, capture_output=True, timeout=120)


def prepare(manifest, data):
    """Download only the explicit public NASA assets in the manifest."""
    data.mkdir(parents=True, exist_ok=True)
    for source in manifest['sources']:
        path = data / (source['id'] + '.mov')
        if not path.exists():
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                response = client.get(source['media_url'])
                response.raise_for_status()
                if len(response.content) > 30_000_000:
                    raise ValueError('Source exceeds benchmark size limit')
                path.write_bytes(response.content)
        for variant in ('clean', 'compressed', 'noise'):
            output = data / f"{source['id']}_{variant}.wav"
            if output.exists():
                continue
            args = ['ffmpeg', '-v', 'error', '-y', '-i', str(path)]
            if variant == 'noise':
                args += ['-f', 'lavfi', '-i', 'anoisesrc=color=pink:amplitude=0.025:sample_rate=16000:seed=42',
                         '-filter_complex', '[0:a][1:a]amix=inputs=2:duration=first:normalize=0']
            elif variant == 'compressed':
                # Telephone bandwidth stress; no claim of a calibrated SNR.
                args += ['-af', 'highpass=f=300,lowpass=f=3400']
            args += ['-vn', '-ac', '1', '-ar', '16000', str(output)]
            command(args)
    print('Prepared nine audio cases from three source assets.', flush=True)


def transcribe(manifest, data, out, providers):
    load_settings()
    for source in manifest['sources']:
        for variant in ('clean', 'compressed', 'noise'):
            case = f"{source['id']}_{variant}"
            media = data / f'{case}.wav'
            for provider in providers:
                destination = out / f'asr_{case}_{provider}.json'
                if destination.exists():
                    continue
                result = {'case': case, 'source_group': source['group'], 'split': source['split'],
                          'provider': provider, 'recorded_at': datetime.now(UTC).isoformat(),
                          'reference_status': 'published_transcript_not_audio_adjudicated',
                          'model': os.getenv('GEMINI_MODEL', 'gemini-flash-latest') if provider == 'gemini' else Path(os.getenv('WHISPER_MODEL', 'small')).name,
                          'api_cost_usd': 0 if provider == 'whisper' else None,
                          'cost_note': 'Local compute excluded; Gemini bill not available from extractor.',
                          'requests_attempted': 0}
                start = time.perf_counter()
                if provider == 'gemini' and not os.getenv('GEMINI_API_KEY'):
                    result['status'] = 'missing_key'
                elif not media.exists():
                    result['status'] = 'missing_media'
                else:
                    try:
                        result['audio_sha256'] = hashlib.sha256(media.read_bytes()).hexdigest()
                        # Isolated command only: select existing production provider without editing .env.
                        original_ask = preprocessing._ask
                        def counted_ask(*args, **kwargs):
                            result['requests_attempted'] += 1
                            return original_ask(*args, **kwargs)
                        with patch('backend.services.specialist.transcription.provider', return_value=provider), patch.object(preprocessing, '_ask', side_effect=counted_ask):
                            features = extract_media_features({'media_id': case, 'local_path': str(media)},
                                {'extract_frames': False, 'extract_visual_text': False})
                        result.update(status='ok', features=features)
                        text = ' '.join(s['text'] for s in features['transcript']['sentences'])
                        result['text'] = text
                        result['published_reference_difference'] = word_error(source['reference'], text)
                        post = {'media': {'media_id': case}, 'title': None, 'caption': None}
                        result['queries'] = build_search_queries(features, post)['queries'][:2]
                    except Exception as exc:
                        # Never persist SDK exceptions, response bodies, URLs containing credentials.
                        result.update(status='error', error_type=type(exc).__name__)
                result['elapsed_seconds'] = round(time.perf_counter() - start, 3)
                write(destination, result)
                print(case, provider, result['status'], result['elapsed_seconds'], flush=True)


def search_one(provider, query, key, client):
    if provider == 'exa':
        response = client.post('https://api.exa.ai/search', headers={'x-api-key': key},
                               json={'query': query, 'type': 'auto', 'numResults': 5})
    else:
        response = client.get('https://serpapi.com/search.json',
                              params={'engine': 'google', 'q': query, 'api_key': key, 'hl': 'en', 'gl': 'us'})
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        raise ValueError('Provider reported failure')
    rows = payload.get('results' if provider == 'exa' else 'organic_results')
    if not isinstance(rows, list):
        # Explicit no-results response only; malformed responses must not score as empty success.
        if provider == 'serpapi' and payload.get('search_information', {}).get('organic_results_state') == 'Fully empty':
            rows = []
        else:
            raise ValueError('Invalid search response')
    return [{'url': r['url' if provider == 'exa' else 'link'], 'title': r.get('title'),
             'query': query, 'query_rank': i} for i, r in enumerate(rows[:5], 1)], payload.get('costDollars', {}).get('total')


def search(manifest, out, query_provider, providers):
    load_settings()
    sources = {s['id']: s for s in manifest['sources']}
    for asr in sorted(out.glob(f'asr_*_{query_provider}.json')):
        item = json.loads(asr.read_text())
        if item['status'] != 'ok':
            continue
        source = sources[item['case'].rsplit('_', 1)[0]]
        for provider in providers:
            destination = out / f"search_{item['case']}_{query_provider}_{provider}.json"
            if destination.exists():
                continue
            result = {'case': item['case'], 'provider': provider, 'query_provider': query_provider,
                      'queries': item['queries'], 'source_group': source['group'], 'split': source['split'],
                      'requests_attempted': 0, 'api_cost_usd': None, 'results': [],
                      'recorded_at': datetime.now(UTC).isoformat(),
                      'equivalent_recording_review': 'pending', 'download_and_alignment': 'not_run'}
            start = time.perf_counter()
            key = os.getenv(KEYS[provider])
            if not key:
                result['status'] = 'missing_key'
            elif not item['queries']:
                result['status'] = 'no_queries'
            else:
                pages, costs = [], []
                try:
                    with httpx.Client(timeout=40) as client:
                        for query in item['queries']:
                            result['requests_attempted'] += 1
                            page, cost = search_one(provider, query, key, client)
                            pages.append(page)
                            costs.append(cost)
                    result.update(status='ok', results=fuse(pages))
                    rank = target_rank(result['results'], source['accepted_urls'])
                    result.update(exact_target_rank=rank, exact_target_top1=rank == 1,
                                  exact_target_top5=rank is not None)
                    if costs and all(c is not None for c in costs):
                        result['api_cost_usd'] = sum(costs)
                except Exception as exc:
                    result.update(status='error', error_type=type(exc).__name__)
            result['elapsed_seconds'] = round(time.perf_counter() - start, 3)
            write(destination, result)
            print(item['case'], provider, result['status'], flush=True)


def report(out):
    rows = [json.loads(p.read_text()) for p in sorted(out.glob('asr_*.json'))]
    searches = [json.loads(p.read_text()) for p in sorted(out.glob('search_*.json'))]
    lines = ['# Quantro benchmark pilot', '',
        'Nine variants of three public NASA assets. These are not nine independent recordings.',
        'Two assets share Kennedy speech content. Famous English archive clips do not represent general social media.',
        'References are NASA published transcripts, not manually adjudicated audio: differences below are provisional, not gold WER.',
        'Single runs include cold model loading. No universal winner or timestamp-accuracy claim is justified.', '',
        '| Case | Provider | Status | Reference difference | Seconds |',
        '|---|---|---|---:|---:|']
    for r in rows:
        rate = r.get('published_reference_difference', {}).get('wer')
        lines.append(f"| {r['case']} | {r['provider']} | {r['status']} | {rate:.1%} | {r['elapsed_seconds']} |" if rate is not None else
                     f"| {r['case']} | {r['provider']} | {r['status']} | — | {r['elapsed_seconds']} |")
    lines += ['', '## Search', '', 'Missing keys/errors are not successful empty results. Exact target URL hits are a lower bound:',
              'equivalent recordings require blind human review. Articles discussing the speech are not recording matches.', '']
    for provider in KEYS:
        subset = [r for r in searches if r['provider'] == provider]
        ok = [r for r in subset if r['status'] == 'ok']
        lines.append(f"- {provider}: {len(ok)}/{len(subset)} completed; " +
                     (f"exact target top-5 {sum(r['exact_target_top5'] for r in ok)}/{len(ok)}." if ok else 'no accuracy score available.'))
    lines += ['', '## Remaining evaluation', '',
              '- Add at least 10 independent recordings across speakers, languages and recording conditions.',
              '- Human-check reference words and timing before selecting an accuracy winner.',
              '- Review candidate recordings, download accessibility, and alignment against known timestamps.',
              '- Keep source groups separate between development and held-out evaluation.',
              '- Re-run paired trials for latency; check provider billing for actual cost.',
              '- Test no-source and unrelated-source cases before claiming reliable discovery.', '']
    (out / 'REPORT.md').write_text('\n'.join(lines))
    print(out / 'REPORT.md')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'transcribe', 'search', 'report'])
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('pilot.json'))
    parser.add_argument('--data', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--out', type=Path, default=DEFAULT_DATA / 'results')
    parser.add_argument('--providers', nargs='+')
    parser.add_argument('--query-provider', choices=['whisper', 'gemini'], default='whisper')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    if args.action == 'prepare':
        prepare(manifest, args.data)
    elif args.action == 'transcribe':
        providers = args.providers or ['whisper', 'gemini']
        if set(providers) - {'whisper', 'gemini'}:
            parser.error('Transcription providers: whisper gemini')
        transcribe(manifest, args.data, args.out, providers)
    elif args.action == 'search':
        providers = args.providers or list(KEYS)
        if set(providers) - set(KEYS):
            parser.error('Search providers: exa serpapi')
        search(manifest, args.out, args.query_provider, providers)
    else:
        report(args.out)


if __name__ == '__main__':
    main()
