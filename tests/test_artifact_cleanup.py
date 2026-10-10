from pathlib import Path
import tempfile
import pytest

from backend.services.pipeline.artifacts import artifact_scope, register_directory


@pytest.mark.parametrize("fail", [False, True])
def test_owned_artifacts_cleaned_but_unregistered_files_kept(tmp_path, fail):
    unrelated = tmp_path / "user-video.mp4"
    unrelated.write_bytes(b"keep")
    created = None
    def pipeline():
        nonlocal created
        with artifact_scope():
            created = Path(tempfile.mkdtemp(dir=tmp_path))
            register_directory(created)
            (created / "frame.jpg").write_bytes(b"frame")
            assert created.exists()
            if fail:
                raise RuntimeError("failed downstream")
    if fail:
        with pytest.raises(RuntimeError):
            pipeline()
    else:
        pipeline()
    assert not created.exists()
    assert unrelated.read_bytes() == b"keep"


def test_nested_jobs_do_not_clean_up_each_others_files(tmp_path):
    with artifact_scope():
        outer = Path(tempfile.mkdtemp(dir=tmp_path)); register_directory(outer)
        with artifact_scope():
            inner = Path(tempfile.mkdtemp(dir=tmp_path)); register_directory(inner)
        assert outer.exists() and not inner.exists()
    assert not outer.exists()
