"""Paddle OCR worker protocol (pii/core/ocr_worker.py).

The framing and the child serve-loop are exercised model-free with fake
streams and a fake ocr_fn; the client (PaddleWorker) is exercised against
inline `python -c` children that speak the protocol without paddle, so the
happy path, per-image errors, and crash/startup surfacing are all covered
without loading a model. One gpu+slow test drives the real paddle worker.
"""

import io
import pickle
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from pii.core.ocr import Box, OcrResult, assemble
from pii.core.ocr_worker import (
    PaddleWorker,
    _OK,
    _READY,
    _read_frame,
    _serve,
    _write_frame,
)

_REPO = Path(__file__).resolve().parents[3]


def _png(size=(20, 12), color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _canned() -> OcrResult:
    return assemble([[("HELLO", Box(0, 0, 10, 10), 90.0)]])


def _child(body: str):
    """A `python -c` child that speaks the worker protocol; REPO is put on
    sys.path so pii.core is importable regardless of cwd."""
    script = (
        f"import sys; sys.path.insert(0, r'{_REPO}')\n"
        "from pii.core.ocr_worker import (_binary_stdio, _read_frame, "
        "_write_frame, _OK, _ERR, _READY)\n"
        "import pickle\n"
        "from pii.core.ocr import assemble, Box\n"
        "r, w = _binary_stdio()\n" + body
    )
    return [sys.executable, "-c", script]


class TestFraming:
    def test_roundtrip(self):
        buf = io.BytesIO()
        _write_frame(buf, _OK, b"payload-bytes")
        buf.seek(0)
        assert _read_frame(buf) == (_OK, b"payload-bytes")

    def test_short_read_is_eof(self):
        buf = io.BytesIO(b"\x00\x00")  # truncated header
        with pytest.raises(EOFError):
            _read_frame(buf)


class TestServe:
    def _run(self, requests: list[bytes], ocr_fn):
        inp = io.BytesIO()
        for payload in requests:
            _write_frame(inp, _OK, payload)
        inp.seek(0)
        out = io.BytesIO()
        _serve(inp, out, ocr_fn)
        out.seek(0)
        frames = []
        while True:
            try:
                frames.append(_read_frame(out))
            except EOFError:
                return frames

    def test_happy_path_returns_ocr_result(self):
        frames = self._run([_png()], lambda image: _canned())
        assert len(frames) == 1
        status, payload = frames[0]
        assert status == _OK
        assert pickle.loads(payload).text == "HELLO"

    def test_bad_image_reports_error_and_keeps_serving(self):
        frames = self._run(
            [b"not-a-png", _png()], lambda image: _canned()
        )
        assert [f[0] for f in frames] == [1, _OK]  # ERR then OK
        assert pickle.loads(frames[1][1]).text == "HELLO"

    def test_ocr_exception_reported_not_fatal(self):
        calls = []

        def flaky(image):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("boom")
            return _canned()

        frames = self._run([_png(), _png()], flaky)
        assert frames[0][0] == 1
        assert b"boom" in frames[0][1]
        assert frames[1][0] == _OK


class TestClient:
    def test_happy_path(self):
        worker = PaddleWorker("test", cmd=_child(
            "_write_frame(w, _READY, b'')\n"
            "_read_frame(r)\n"
            "_write_frame(w, _OK, pickle.dumps("
            "assemble([[('HELLO', Box(0, 0, 10, 10), 90.0)]])))\n"
        ))
        try:
            result = worker.ocr(Image.new("RGB", (20, 12), "white"))
            assert result.text == "HELLO"
        finally:
            worker.close()

    def test_per_image_error_surfaces(self):
        worker = PaddleWorker("test", cmd=_child(
            "_write_frame(w, _READY, b'')\n"
            "_read_frame(r)\n"
            "_write_frame(w, _ERR, b'kaboom on image')\n"
        ))
        try:
            with pytest.raises(RuntimeError, match="kaboom on image"):
                worker.ocr(Image.new("RGB", (20, 12), "white"))
        finally:
            worker.close()

    def test_startup_failure_surfaces(self):
        # child exits before READY -> clear error at construction, no hang.
        with pytest.raises(RuntimeError, match="failed to start|died"):
            PaddleWorker("test", cmd=_child("_write_frame(w, _ERR, b'load failed')\n"))

    def test_dead_worker_mid_call_raises(self):
        worker = PaddleWorker("test", cmd=_child(
            "_write_frame(w, _READY, b'')\n"
            "_read_frame(r)\n"
            "sys.exit(3)\n"  # read the request then die without responding
        ))
        try:
            with pytest.raises(RuntimeError, match="died"):
                worker.ocr(Image.new("RGB", (20, 12), "white"))
        finally:
            worker.close()


@pytest.mark.gpu
@pytest.mark.slow
def test_real_paddle_worker_end_to_end():
    """The real worker: PNG in, readable OcrResult out (paddle GPU)."""
    from PIL import ImageDraw, ImageFont

    from pii.core.ocr_worker import worker_ocr

    arial = Path(r"C:\Windows\Fonts\arial.ttf")
    if not arial.exists():
        pytest.skip("needs arial.ttf")
    img = Image.new("RGB", (600, 100), "white")
    ImageDraw.Draw(img).text(
        (40, 30), "TFN 123 456 782", font=ImageFont.truetype(str(arial), 32),
        fill="black",
    )
    result = worker_ocr("v6_medium", img)
    assert "123 456 782" in result.text


def test_worker_module_is_torch_free_to_import():
    """Importing the client side must not drag paddle/torch in (the parent
    is a torch process)."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s')\n"
         "import pii.core.ocr_worker\n"
         "assert 'paddle' not in sys.modules\n"
         "print('ok')" % _REPO],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
