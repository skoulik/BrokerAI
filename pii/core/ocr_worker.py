"""Persistent PaddleOCR worker subprocess (both sides of the protocol).

Why a subprocess at all: on Windows the GPU paddle wheel and torch cannot
share a process — both bundle cudnn from different CUDA families and the
second loader gets WinError 127 (full story in ocr_paddle.py). The full
pii pipeline runs GLiNER2 on torch in-process, so with Tesseract retired
paddle-GPU has to live somewhere torch never loads: its own interpreter.

Design:
- One worker per model tier, spawned lazily on first use and kept alive
  for the whole run (the PaddleOCR engine loads once, not per call).
- Framed request/response over the child's stdio: parent writes a PNG
  frame to the child's stdin, child writes a serialized OcrResult frame
  back. The child claims fd 1 for the protocol and redirects Python/C
  stdout to stderr FIRST, so paddle's chatty logging can never corrupt
  the binary stream (both fds forced to binary mode on Windows).
- Crash surfacing: a dead child closes the pipe, so a short read returns
  EOF and the client raises a clear RuntimeError with the exit code
  instead of hanging. A startup handshake (the child sends READY once its
  engine is loaded) turns an engine-load failure into an error at spawn
  time, not on the first image.

The child imports paddle; the parent (client) side imports only stdlib,
so `import pii.core.ocr_worker` in a torch process stays torch-safe — the
paddle import lives inside `main()`, reached only as `python -m
pii.core.ocr_worker <tier>`.
"""

import atexit
import io
import os
import pickle
import struct
import subprocess
import sys
import threading

from PIL import Image

from pii.core.ocr import OcrResult

# Frame: 1 status byte + 4-byte big-endian length + payload.
_OK = 0
_ERR = 1
_READY = 2
_HEADER = struct.Struct(">BI")


def _write_frame(stream, status: int, payload: bytes) -> None:
    stream.write(_HEADER.pack(status, len(payload)))
    stream.write(payload)
    stream.flush()


def _read_exactly(stream, n: int) -> bytes:
    """Read exactly n bytes or raise EOFError (a short read means the peer
    closed the pipe — i.e. the worker died)."""
    chunks = []
    remaining = n
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError(f"pipe closed with {remaining} of {n} bytes unread")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream) -> tuple[int, bytes]:
    status, length = _HEADER.unpack(_read_exactly(stream, _HEADER.size))
    return status, _read_exactly(stream, length)


# --------------------------------------------------------------------------
# Child side
# --------------------------------------------------------------------------

def _serve(read_stream, write_stream, ocr_fn) -> None:
    """Frame loop: PNG in, serialized OcrResult out, until stdin closes.

    A per-image failure is returned as an error frame and the worker keeps
    serving (one bad page must not kill the engine); only stdin EOF ends
    the loop. `ocr_fn` is injected so this loop is testable without paddle.
    """
    while True:
        try:
            status, payload = _read_frame(read_stream)
        except EOFError:
            return  # parent closed stdin — clean shutdown
        try:
            image = Image.open(io.BytesIO(payload))
            image.load()
            result = ocr_fn(image)
            _write_frame(write_stream, _OK, pickle.dumps(
                result, protocol=pickle.HIGHEST_PROTOCOL))
        except Exception:  # noqa: BLE001 - reported to the parent, not fatal
            import traceback
            _write_frame(write_stream, _ERR,
                         traceback.format_exc().encode("utf-8", "replace"))


def _binary_stdio() -> tuple:
    """Claim fd 1 for the protocol, redirect Python+C stdout to stderr, and
    force both protocol fds to binary. Returns (read_stream, write_stream)."""
    proto_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())  # paddle noise -> stderr
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(proto_fd, os.O_BINARY)
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    return os.fdopen(sys.stdin.fileno(), "rb", closefd=False), \
        os.fdopen(proto_fd, "wb")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    tier = argv[0]
    read_stream, write_stream = _binary_stdio()
    try:
        from functools import partial

        from pii.core.ocr_paddle import _engine, ocr_image_paddle

        _engine(tier)  # load once; surfaces a load failure before READY
        ocr_fn = partial(ocr_image_paddle, tier=tier)
    except Exception:
        import traceback
        _write_frame(write_stream, _ERR,
                     traceback.format_exc().encode("utf-8", "replace"))
        return 1
    _write_frame(write_stream, _READY, b"")
    _serve(read_stream, write_stream, ocr_fn)
    return 0


# --------------------------------------------------------------------------
# Parent side (client)
# --------------------------------------------------------------------------

def _png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class PaddleWorker:
    """Handle to one worker subprocess for a single tier.

    Not concurrency-safe: a single request/response occupies the pipe, so
    callers serialize access with a lock (the module-level pool does).
    """

    def __init__(self, tier: str, cmd=None):
        self.tier = tier
        self.proc = subprocess.Popen(
            cmd or [sys.executable, "-m", "pii.core.ocr_worker", tier],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        self._lock = threading.Lock()
        status, payload = self._recv()  # handshake: wait for READY
        if status != _READY:
            self.close()
            raise RuntimeError(
                f"paddle worker ({tier}) failed to start:\n"
                f"{payload.decode('utf-8', 'replace')}")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _recv(self) -> tuple[int, bytes]:
        try:
            return _read_frame(self.proc.stdout)
        except EOFError as e:
            code = self.proc.wait()
            raise RuntimeError(
                f"paddle worker ({self.tier}) died (exit {code}) — {e}"
            ) from None

    def ocr(self, image: Image.Image) -> OcrResult:
        with self._lock:
            if not self.alive():
                raise RuntimeError(
                    f"paddle worker ({self.tier}) is not running "
                    f"(exit {self.proc.returncode})")
            try:
                _write_frame(self.proc.stdin, _OK, _png_bytes(image))
            except (BrokenPipeError, OSError) as e:
                code = self.proc.wait()
                raise RuntimeError(
                    f"paddle worker ({self.tier}) died (exit {code}) — {e}"
                ) from None
            status, payload = self._recv()
        if status == _OK:
            return pickle.loads(payload)
        raise RuntimeError(
            f"paddle worker ({self.tier}) failed on an image:\n"
            f"{payload.decode('utf-8', 'replace')}")

    def close(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.close()  # EOF -> clean loop exit
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


_pool: dict[str, PaddleWorker] = {}
_pool_lock = threading.Lock()


def worker_ocr(tier: str, image: Image.Image) -> OcrResult:
    """OCR one image through the persistent worker for `tier`, spawning it
    on first use. A worker that died between calls is replaced (a fresh
    document gets a fresh attempt); a death mid-call surfaces from ocr()."""
    with _pool_lock:
        worker = _pool.get(tier)
        if worker is None or not worker.alive():
            worker = PaddleWorker(tier)
            _pool[tier] = worker
    return worker.ocr(image)


@atexit.register
def _shutdown_pool() -> None:
    for worker in _pool.values():
        worker.close()
    _pool.clear()


if __name__ == "__main__":
    sys.exit(main())
