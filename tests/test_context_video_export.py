"""Real FFmpeg contract tests; no external network or database required."""
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock

import pytest
from backend.services.resolution import context_video as cv


@pytest.fixture
def source(tmp_path):
    path=tmp_path/'source.mp4'
    cv._run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=navy:s=320x240:r=30:d=8',
             '-f','lavfi','-i','sine=frequency=440:duration=8','-c:v','libx264','-c:a','aac','-shortest',str(path)])
    return path


def test_real_export_codecs_timing_labels_and_faststart(source,tmp_path):
    output=tmp_path/'context.mp4'
    selection={'start_seconds':1.,'end_seconds':7.}
    alignment=N(matches=[N(source_start_seconds=3.,source_end_seconds=5.)])
    assert abs(cv.encode(source,output,selection,alignment)-6)<.05
    data=output.read_bytes()
    assert data.index(b'moov')<data.index(b'mdat')
    # Distinct blue/green label pixels are present at each stage of the actual MP4.
    colors=[]
    for t in [1,3,5]:
        frame=tmp_path/f'{t}.png'
        cv._run(['ffmpeg','-y','-v','error','-ss',str(t),'-i',str(output),'-frames:v','1',str(frame)])
        from PIL import Image
        colors.append(Image.open(frame).convert('RGB').getpixel((23,35)))
    assert colors[0][2]>colors[0][1] and colors[2][2]>colors[2][1]
    assert colors[1][1]>colors[1][2]


def test_silent_input_and_invalid_bounds(source,tmp_path):
    silent=tmp_path/'silent.mp4'
    cv._run(['ffmpeg','-y','-v','error','-i',str(source),'-an','-c:v','copy',str(silent)])
    alignment=N(matches=[N(source_start_seconds=3.,source_end_seconds=5.)])
    out=tmp_path/'silent-context.mp4'
    cv.encode(silent,out,{'start_seconds':1.,'end_seconds':7.},alignment)
    assert any(s['codec_type']=='audio' for s in cv._probe(out)['streams'])
    with pytest.raises(ValueError):
        cv.encode(source,tmp_path/'invalid.mp4',{'start_seconds':-1.,'end_seconds':7.},alignment)


def test_persists_before_return_and_cleans_scratch(source,monkeypatch):
    from backend.services.shared.types import Post,MediaAsset,Author,Engagement,Retrieval
    post=Post(post_id='source',post_url='https://youtube.com/watch?v=e50Tt9qJRQk',platform='youtube',
              author=Author(),engagement=Engagement(),media=MediaAsset(media_id='source',mime_type='video/mp4',local_path=str(source)),
              retrieval=Retrieval(retrieved_at='2026-10-11T00:00:00Z',provider='test'))
    captured=[]
    class Store:
        closed=False
        def save_post(self,p):
            captured.append(Path(p.media.local_path))
            assert captured[-1].is_file()
            assert p.media.media_id.startswith('context_')
        def close(self): self.closed=True
    store=Store()
    monkeypatch.setattr(cv.TigerDataPostStore,'from_env',lambda:store)
    asset=asyncio.run(cv.export(post,N(matches=[N(source_start_seconds=3.,source_end_seconds=5.)]),{'start_seconds':1.,'end_seconds':7.}))
    assert asset.media_id.startswith('context_') and store.closed
    assert not captured[0].parent.exists()
    assert 'local_path' not in asset.model_dump()


def test_failed_encoding_cleans_temp_and_never_exposes_asset(monkeypatch):
    folders=[]
    async def download(url,dest_dir):
        folders.append(Path(dest_dir))
        path=Path(dest_dir)/'bad.mp4';path.write_bytes(b'invalid')
        return {'file_path':str(path)}
    monkeypatch.setattr(cv,'download_short_media',download)
    post=N(media=N(local_path=''),post_url='https://youtube.com/watch?v=e50Tt9qJRQk')
    with pytest.raises(subprocess.CalledProcessError):
        asyncio.run(cv.export(post,N(matches=[]),{}))
    assert folders and not folders[0].exists()


def test_separate_matches_leave_source_gap_labeled_as_context(source, tmp_path):
    from PIL import Image
    output = tmp_path / 'gapped.mp4'
    matches = N(matches=[N(source_start_seconds=2., source_end_seconds=3.),
                         N(source_start_seconds=5., source_end_seconds=6.)])
    cv.encode(source, output, {'start_seconds':1., 'end_seconds':7.}, matches)
    colors = []
    for timestamp in (1.5, 3., 4.5):
        frame = tmp_path / f'gap-{timestamp}.png'
        cv._run(['ffmpeg', '-y', '-v', 'error', '-ss', str(timestamp), '-i', str(output), '-frames:v', '1', str(frame)])
        with Image.open(frame) as image:
            colors.append(image.convert('RGB').getpixel((23, 35)))
    assert colors[0][1] > colors[0][2] and colors[2][1] > colors[2][2]
    assert colors[1][2] > colors[1][1]
