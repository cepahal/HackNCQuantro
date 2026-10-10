"""Separate-stream retrieval preserves budgets and a local-only merge boundary."""
import json
import shutil
import struct
import subprocess
import time
from pathlib import Path

import pytest
import yt_dlp
from backend.services.external_apis import _retrieval_worker as worker
from backend.services.external_apis import retrieve_post as retrieval


def container():
    return b''.join(struct.pack('>I4s', 8, name) for name in (b'ftyp', b'moov', b'mdat'))


@pytest.mark.parametrize('failure', ['oversize', 'symlink', 'invalid', 'merge', 'deadline'])
def test_merge_rejects_bad_inputs_and_failures(tmp_path, monkeypatch, failure):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if '--split-media' in command:
            for name in ('video.mp4', 'audio.m4a'):
                (tmp_path / name).write_bytes(b'bad' if failure == 'invalid' else container())
            if failure == 'symlink':
                (tmp_path / 'audio.m4a').unlink()
                (tmp_path / 'audio.m4a').symlink_to(tmp_path / 'video.mp4')
            return subprocess.CompletedProcess(command, 0, '{}', '')
        return subprocess.CompletedProcess(command, 1, b'', b'private diagnostic')
    monkeypatch.setattr(retrieval.subprocess, 'run', run)
    with pytest.raises((retrieval.RetrievalError, subprocess.TimeoutExpired)) as caught:
        retrieval._retrieve_split_media('https://youtube.com/watch?v=test', tmp_path,
                                       30 if failure == 'oversize' else 100,
                                       time.monotonic() + (-1 if failure == 'deadline' else 5))
    assert 'private diagnostic' not in str(caught.value)
    if failure != 'merge':
        assert not any(command[0] == 'ffmpeg' for command in calls)


def test_real_local_merge_retains_audio_and_video(tmp_path, monkeypatch):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg not installed')
    original_run = subprocess.run
    def run(command, **kwargs):
        if '--split-media' in command:
            original_run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=64x64:duration=1',
                          '-c:v','libx264',str(tmp_path/'video.mp4')],check=True,capture_output=True)
            original_run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=1',
                          '-c:a','aac',str(tmp_path/'audio.m4a')],check=True,capture_output=True)
            return subprocess.CompletedProcess(command,0,'{"title":"Test"}','')
        assert command.count('-protocol_whitelist') == 2
        assert command.count('mov') == 2
        assert 'http' not in ' '.join(command)
        assert kwargs['timeout'] <= 30
        return original_run(command,**kwargs)
    monkeypatch.setattr(retrieval.subprocess,'run',run)
    assert json.loads(retrieval._retrieve_split_media('https://youtube.com/watch?v=test',tmp_path,100000,time.monotonic()+30))['title']=='Test'
    merged=tmp_path/'media.mp4'
    assert retrieval._is_complete_mp4(merged,merged.stat().st_size)
    probe=original_run(['ffprobe','-v','error','-show_entries','stream=codec_type','-of','json',str(merged)],capture_output=True,text=True,check=True)
    assert {stream['codec_type'] for stream in json.loads(probe.stdout)['streams']}=={'video','audio'}
    assert not (tmp_path/'video.mp4').exists()
    assert not (tmp_path/'audio.m4a').exists()


@pytest.mark.parametrize('mode', ['success', 'combined_limit', 'progress_limit', 'unsafe_protocol', 'live'])
def test_worker_downloads_fixed_paths_with_shared_budget(tmp_path,monkeypatch,capsys,mode):
    class Downloader:
        def __init__(self,params):self.params=params
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,url,download):
            assert download is False
            return {'is_live': mode=='live','requested_formats':[
                {'url':'https://video.example/media','protocol':'http' if mode=='unsafe_protocol' else 'https','ext':'mp4','vcodec':'avc1'},
                {'url':'https://audio.example/media','protocol':'https','ext':'m4a','acodec':'aac'}]}
        def dl(self,name,info):
            assert Path(name).parent==tmp_path
            assert Path(name).name in {'video.mp4','audio.m4a'}
            assert info['requested_formats'] is None
            self.params['progress_hooks'][0]({'downloaded_bytes':101 if mode=='progress_limit' else 40})
            Path(name).write_bytes(b'x'*(60 if mode=='combined_limit' else 40))
            return True,True
        def sanitize_info(self,info):return info
    monkeypatch.setattr(yt_dlp,'YoutubeDL',Downloader)
    args=['--split-media','https://youtube.com/watch?v=test','--workspace',str(tmp_path),'--max-bytes','100']
    if mode=='success':
        worker._split_media_main(args)
        assert json.loads(capsys.readouterr().out)['acodec']=='aac'
    else:
        with pytest.raises(ValueError):worker._split_media_main(args)
