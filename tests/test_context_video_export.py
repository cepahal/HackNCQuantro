"""Real FFmpeg contract tests; no external network or database required."""
import asyncio
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
    from backend.services.shared.types import (
        Author,
        Engagement,
        MediaAsset,
        Post,
        Retrieval,
    )
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


@pytest.mark.parametrize("value", [None, "N/A", "nan", "inf", "0", "-1"])
def test_duration_uses_container_when_stream_duration_is_unusable(value):
    assert cv._duration({"duration": value}, {"format": {"duration": "8.0"}}) == 8.0


def test_duration_rejects_missing_or_nonfinite_metadata():
    with pytest.raises(ValueError, match="duration is unavailable"):
        cv._duration({"duration": "N/A"}, {"format": {"duration": "nan"}})


def test_empty_alignment_is_rejected_before_ffmpeg_encoding(source, tmp_path):
    with pytest.raises(ValueError, match="No aligned passages"):
        cv.encode(source, tmp_path / "empty.mp4", {"start_seconds": 0., "end_seconds": 7.}, N(matches=[]))
    assert not (tmp_path / "empty.mp4").exists()


def test_export_recovers_tiger_source_without_network(monkeypatch):
    import hashlib
    data = b"stored mp4 bytes"
    class Store:
        closed = False
        def read_media(self, media_id):
            assert media_id == 'stored_source'
            return 'video/mp4', data, hashlib.sha256(data).hexdigest()
        def close(self): self.closed = True
    store = Store()
    monkeypatch.setattr(cv.TigerDataPostStore, 'from_env', lambda: store)
    download = AsyncMock(side_effect=AssertionError('network must not be used'))
    monkeypatch.setattr(cv, 'download_short_media', download)
    paths = []
    def encode(post, path, output, selection, alignment):
        paths.append(Path(path))
        assert paths[-1].read_bytes() == data and store.closed
        return 'stored_asset'
    monkeypatch.setattr(cv, '_encode_and_store', encode)
    post = N(media=N(local_path='', media_id='stored_source'), post_url='https://example.com/source')
    assert asyncio.run(cv.export(post, N(matches=[]), {})) == 'stored_asset'
    download.assert_not_awaited()
    assert not paths[0].exists()


@pytest.mark.parametrize('mime,digest', [('image/jpeg', 'valid'), ('video/mp4', 'corrupt')])
def test_cached_source_rejects_wrong_mime_or_digest(monkeypatch, tmp_path, mime, digest):
    import hashlib
    data = b'cached bytes'
    class Store:
        closed = False
        def read_media(self, media_id):
            return mime, data, hashlib.sha256(data).hexdigest() if digest == 'valid' else '0' * 64
        def close(self): self.closed = True
    store = Store()
    monkeypatch.setattr(cv.TigerDataPostStore, 'from_env', lambda: store)
    output = tmp_path / 'cached.mp4'
    assert cv._cached_source('source', output) is None
    assert store.closed and not output.exists()


@pytest.mark.parametrize('mode,reason', [('miss', 'cache_miss'), ('failed', 'cache_read_failed'),
                                      ('invalid', 'integrity_or_type_invalid'), ('hit', 'cache_hit')])
def test_source_cache_diagnostics_are_fixed_safe_reasons(monkeypatch, tmp_path, mode, reason):
    import hashlib
    data = b'media bytes'
    class Store:
        closed = False
        def read_media(self, media_id):
            if mode == 'failed':
                raise RuntimeError('secret=private /tmp/cache.mp4')
            if mode == 'miss':
                return None
            return ('image/jpeg' if mode == 'invalid' else 'video/mp4', data, hashlib.sha256(data).hexdigest())
        def close(self):
            self.closed = True
    store = Store()
    monkeypatch.setattr(cv.TigerDataPostStore, 'from_env', lambda: store)
    events = []
    monkeypatch.setattr(cv, 'record', lambda *args: events.append(args))
    output = tmp_path / 'source.mp4'
    result = cv._cached_source('source_media', output)
    assert bool(result) is (mode == 'hit')
    assert store.closed
    assert events == [('context_source_cache', 'completed' if mode == 'hit' else 'unavailable',
                       {'media_id': 'source_media', 'reason': reason})]
    assert 'secret' not in str(events) and '/tmp/' not in str(events)


def test_existing_local_source_has_priority_over_tiger(monkeypatch, tmp_path):
    path = tmp_path / 'source.mp4'
    path.write_bytes(b'local')
    monkeypatch.setattr(cv, '_cached_source', lambda *a: pytest.fail('cache must not be read'))
    monkeypatch.setattr(cv, 'download_short_media', AsyncMock(side_effect=AssertionError('network must not be used')))
    monkeypatch.setattr(cv, '_encode_and_store', lambda post, source, *a: str(source))
    post = N(media=N(local_path=str(path), media_id='source'), post_url='https://example.com/source')
    assert asyncio.run(cv.export(post, N(matches=[]), {})) == str(path)
    assert path.exists()


def test_tiger_failure_falls_back_to_guarded_download(monkeypatch):
    def unavailable():
        raise RuntimeError('private storage diagnostic')
    monkeypatch.setattr(cv.TigerDataPostStore, 'from_env', unavailable)
    async def download(url, dest_dir):
        path = Path(dest_dir) / 'download.mp4'
        path.write_bytes(b'download')
        return {'file_path': str(path)}
    mock = AsyncMock(side_effect=download)
    monkeypatch.setattr(cv, 'download_short_media', mock)
    monkeypatch.setattr(cv, '_encode_and_store', lambda post, path, *a: Path(path).read_bytes())
    post = N(media=N(local_path='', media_id='source'), post_url='https://example.com/source')
    assert asyncio.run(cv.export(post, N(matches=[]), {})) == b'download'
    mock.assert_awaited_once()


def test_probe_rejects_network_playlist(tmp_path):
    # Do not follow URLs or local references from autodetected playlist formats.
    path = tmp_path / 'untrusted.m3u8'
    path.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\nhttp://127.0.0.1:9/private.ts\n#EXT-X-ENDLIST\n')
    with pytest.raises(subprocess.CalledProcessError) as error:
        cv._probe(path)
    assert 'whitelist' in error.value.stderr.lower()


def test_probe_and_encode_restrict_source_protocols(source, tmp_path, monkeypatch):
    commands = []
    run = cv._run
    def capture(args):
        commands.append(args)
        return run(args)
    monkeypatch.setattr(cv, '_run', capture)
    cv.encode(source, tmp_path / 'restricted.mp4', {'start_seconds': 0., 'end_seconds': 4.},
              N(matches=[N(source_start_seconds=1., source_end_seconds=3.)]))
    for command in commands:
        assert command[command.index('-protocol_whitelist') + 1] == 'file'
        assert command[command.index('-format_whitelist') + 1] == 'mov,matroska,webm'
    assert any(command[0] == 'ffmpeg' for command in commands)
