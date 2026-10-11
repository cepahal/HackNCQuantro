"""Sentence-bounded original-source export, persisted via the existing media store."""
import asyncio
import json
import math
import subprocess
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace as N

from . import sentence_context
from .alignment import _coverage
from .context_badge import render_cue
from .download import _find_tool, download_short_media
from ..external_apis.tigerdata_storage import TigerDataPostStore
from ..shared.types import Context, MediaAsset, PublicMediaAsset


def select(alignment, features):
    adapted = N(matches=[N(source_start=m.source_start_seconds, source_end=m.source_end_seconds)
                         for m in alignment.matches], source=N(duration=features.duration_seconds),
                source_transcript=N(sentences=[N(start=s.start_seconds,end=s.end_seconds,text=s.text)
                                               for s in features.transcript.sentences]))
    selection = sentence_context.select_context(adapted, chooser=sentence_context.model_counts)
    start, end = selection['start_seconds'], selection['end_seconds']
    context = Context(source_media_id=features.media_id, windows=[{
        'context_id':'context_001', 'match_ids':[m.match_id for m in alignment.matches],
        'requested_start_seconds':start, 'requested_end_seconds':end,
        'coverage':_coverage(start,end,features),
        'sentences':[s for s in features.transcript.sentences if s.start_seconds<end and s.end_seconds>start],
    }], limitations=[*features.warnings, selection['timing_note']])
    if context.windows[0].coverage != 'complete':
        context.limitations.append('Surrounding transcript coverage is incomplete; missing speech is not silence.')
    return context, selection


def _run(args):
    tool = _find_tool(args[0])
    if not tool:
        raise RuntimeError('Media tools are unavailable')
    return subprocess.run([tool,*args[1:]], check=True, capture_output=True, text=True, timeout=120)


def _probe(path):
    return json.loads(_run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]).stdout)


def encode(source_path, output, selection, alignment):
    """One encode, continuous source audio/video, with timed labels burned in."""
    info = _probe(source_path)
    video = next((s for s in info['streams'] if s['codec_type']=='video'),None)
    if not video:
        raise ValueError('Source has no video stream')
    duration = float(video.get('duration') or info['format']['duration'])
    start, end = selection['start_seconds'], min(selection['end_seconds'],duration)
    first = min(m.source_start_seconds for m in alignment.matches)
    last = max(m.source_end_seconds for m in alignment.matches)
    if not all(math.isfinite(v) for v in (start,end,first,last,duration)) or not 0<=start<=first<last<=end or end-start>300:
        raise ValueError('Invalid or excessive export range')
    # Merge overlapping passages, but never label omitted source speech as clip content.
    intervals = []
    for match in sorted(alignment.matches, key=lambda m: m.source_start_seconds):
        a, b = match.source_start_seconds, match.source_end_seconds
        if not all(math.isfinite(v) for v in (a, b)) or not start <= a < b <= end:
            raise ValueError('Invalid matched passage')
        if intervals and a <= intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], b)
        else:
            intervals.append([a, b])
    spans, cursor = [], start
    for a, b in intervals:
        if a > cursor:
            spans.append(('CONTEXT BEFORE' if cursor == start else 'CONTEXT BETWEEN', cursor, a, '#38bdf8'))
        spans.append(('YOUR CLIP’S PASSAGE', a, b, '#34d399'))
        cursor = b
    if end > cursor:
        spans.append(('CONTEXT AFTER', cursor, end, '#38bdf8'))
    cues = [{'label':label,'start_seconds':a-start,'end_seconds':b-start,'color':color}
            for label,a,b,color in spans]
    args = ['ffmpeg','-nostdin','-y','-v','error','-ss',str(start),'-i',str(source_path)]
    filters = ['[0:v]setpts=PTS-STARTPTS,scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v0]']
    for i,cue in enumerate(cues):
        badge = Path(output).parent / f'cue_{i}.png'
        render_cue(badge,cue,640)
        args += ['-i',str(badge)]
        filters.append(f"[v{i}][{i+1}:v]overlay=x=12:y=12:enable='gte(t,{cue['start_seconds']})*lt(t,{cue['end_seconds']})'[v{i+1}]")
    audio = '0:a:0'
    if not any(s['codec_type']=='audio' for s in info['streams']):
        args += ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
        audio = f'{len(cues)+1}:a:0'
    args += ['-filter_complex_threads','1','-filter_complex',';'.join(filters),'-map',f'[v{len(cues)}]',
             '-map',audio,'-af','asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,apad',
             '-t',str(end-start),'-c:v','libx264','-threads','2','-preset','veryfast','-crf','20',
             '-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-movflags','+faststart',str(output)]
    _run(args)
    result = _probe(output)
    streams = result['streams']
    v = next(s for s in streams if s['codec_type']=='video')
    a = next(s for s in streams if s['codec_type']=='audio')
    actual = float(v['duration'])
    if v['codec_name']!='h264' or a['codec_name']!='aac' or abs(actual-(end-start))>.1 or abs(actual-float(a['duration']))>.1:
        raise ValueError('Export failed stream verification')
    return actual


def _encode_and_store(source, source_path, output, selection, alignment):
    duration = encode(source_path,output,selection,alignment)
    identity = 'context_' + uuid.uuid4().hex
    asset = MediaAsset(media_id=identity,local_path=str(output),mime_type='video/mp4',
                       duration_seconds=duration,width=640,height=360,fps=30.,has_audio=True)
    stored = source.model_copy(deep=True,update={'post_id':'post_'+identity,'media':asset})
    store = TigerDataPostStore.from_env()
    try:
        store.save_post(stored)
    finally:
        store.close()
    return PublicMediaAsset.model_validate(asset.model_dump(exclude={'local_path'}))


async def export(source, alignment, selection):
    """Only expose a media ID after durable storage succeeds; always clean scratch."""
    with tempfile.TemporaryDirectory(prefix='quantro-context-') as td:
        path = source.media.local_path
        if not path or not Path(path).is_file():
            download = await download_short_media(source.post_url,dest_dir=td)
            path = download['file_path']
        task = asyncio.create_task(asyncio.to_thread(_encode_and_store,source,path,Path(td)/'context.mp4',selection,alignment))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not delete inputs while the encoding/storage worker still owns them.
            await asyncio.gather(task,return_exceptions=True)
            raise
