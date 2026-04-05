#!/usr/bin/env python3
"""
tools_server.py — Karaoke Vocal Trainer companion server v2.3.0
Wraps Demucs (source separation) and Basic Pitch (audio→MIDI) via CLI.
Uses ONLY Python standard library. No Flask, no fastapi, no mido, no numpy.
Run: python tools_server.py
"""

# ─── CONFIGURATION (change these if needed) ──────────────────────────────────
import os as _os
PORT                = int(_os.environ.get('PORT',                '5050'))
DEMUCS_MODEL        =     _os.environ.get('DEMUCS_MODEL',        'htdemucs_ft')
FFMPEG_CMD          =     _os.environ.get('FFMPEG_CMD',          'ffmpeg')
CONVERT_SAMPLE_RATE = int(_os.environ.get('CONVERT_SAMPLE_RATE', '44100'))
CONVERT_CHANNELS    = int(_os.environ.get('CONVERT_CHANNELS',    '2'))
# ALLOW_ORIGIN: '*' allows all origins (needed for Docker); restrict for production
ALLOW_ORIGIN        =     _os.environ.get('ALLOW_ORIGIN',        '*')
LISTEN_HOST         =     _os.environ.get('LISTEN_HOST',         '0.0.0.0')
MAX_JOB_AGE_SEC     = int(_os.environ.get('MAX_JOB_AGE_SEC',     '7200'))   # 2 hours default
DEMUCS_TIMEOUT_SEC  = int(_os.environ.get('DEMUCS_TIMEOUT_SEC', '2700'))  # 45 min kill timeout
DEMUCS_THREADS      = int(_os.environ.get('DEMUCS_THREADS',      '2'))    # OMP threads (keep < CPU count to avoid 100% load)
# ─────────────────────────────────────────────────────────────────────────────

import io
import json
os = _os  # alias (imported above as _os)
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a separate thread."""
    daemon_threads = True   # threads die when main thread exits

from pathlib import Path
from urllib.parse import urlparse

TMP_BASE = Path(__file__).parent / 'tmp'
TMP_BASE.mkdir(exist_ok=True)

# ─── Utility ─────────────────────────────────────────────────────────────────

def _write_status(job_dir: Path, status: str, step: str, pct: int,
                  files: dict = None, error: str = None, pid: int = None):
    """Atomically write status.json for a job."""
    data = {
        'status': status,
        'step':   step,
        'pct':    pct,
        'files':  files or {},
        'error':  error,
    }
    if pid is not None:
        data['pid'] = pid
    tmp_path = job_dir / 'status.json.tmp'
    tmp_path.write_text(json.dumps(data), encoding='utf-8')
    tmp_path.replace(job_dir / 'status.json')


def _read_status(job_dir: Path) -> dict:
    """Read status.json; return error dict if missing."""
    p = job_dir / 'status.json'
    if not p.exists():
        return {'status': 'error', 'step': 'unknown', 'pct': 0,
                'files': {}, 'error': 'status file not found'}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        return {'status': 'error', 'step': 'unknown', 'pct': 0,
                'files': {}, 'error': str(e)}


def _safe_filename(name: str) -> bool:
    """Return True if filename is safe (no path traversal)."""
    return bool(name) and '/' not in name and '\\' not in name and '..' not in name


def _check_tool(cmd: list, timeout: int = 3) -> bool:
    """Return True if a CLI tool is available."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode == 0 or True  # some tools exit 1 for --version
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _cleanup_old_jobs():
    """Delete job directories older than MAX_JOB_AGE_SEC."""
    now = time.time()
    cleaned = 0
    for child in TMP_BASE.iterdir():
        if child.is_dir():
            try:
                age = now - child.stat().st_mtime
                if age > MAX_JOB_AGE_SEC:
                    shutil.rmtree(child, ignore_errors=True)
                    cleaned += 1
            except OSError:
                pass
    if cleaned:
        print(f'[tools_server] Cleaned up {cleaned} old job(s) from tmp/', flush=True)


def _start_cleanup_thread():
    """Run _cleanup_old_jobs every 30 minutes in background."""
    def _loop():
        while True:
            time.sleep(1800)  # 30 minutes
            try:
                _cleanup_old_jobs()
            except Exception as e:
                print(f'[tools_server] Cleanup error: {e}', flush=True)
    t = threading.Thread(target=_loop, daemon=True, name='cleanup')
    t.start()


def _parse_multipart(handler) -> tuple[dict, dict]:
    """
    Parse multipart/form-data WITHOUT the deprecated cgi module.
    Pure stdlib implementation (re + io).
    Returns (fields, files) where:
      fields[name] = str value
      files[name]  = {'filename': str, 'data': bytes, 'content_type': str}
    """
    import re as _re

    content_type   = handler.headers.get('Content-Type', '')
    content_length = int(handler.headers.get('Content-Length', 0))
    body           = handler.rfile.read(content_length)

    # Extract boundary from Content-Type header
    m = _re.search(r'boundary=([^\s;]+)', content_type)
    if not m:
        raise ValueError(f'No boundary in Content-Type: {content_type}')
    boundary = m.group(1).strip('"').encode()

    fields: dict = {}
    files:  dict = {}

    # Split body on --boundary
    delimiter = b'--' + boundary
    parts = body.split(delimiter)
    # parts[0] is preamble (empty), parts[-1] is '--\r\n' epilogue

    for part in parts[1:-1]:
        # Each part: \r\n<headers>\r\n\r\n<body>\r\n
        if part.startswith(b'\r\n'):
            part = part[2:]
        if part.endswith(b'\r\n'):
            part = part[:-2]

        # Split headers from body at first blank line
        if b'\r\n\r\n' in part:
            raw_headers, data = part.split(b'\r\n\r\n', 1)
        elif b'\n\n' in part:
            raw_headers, data = part.split(b'\n\n', 1)
        else:
            continue

        # Parse part headers
        header_lines = raw_headers.decode('utf-8', errors='replace').splitlines()
        part_headers: dict = {}
        for line in header_lines:
            if ':' in line:
                k, v = line.split(':', 1)
                part_headers[k.strip().lower()] = v.strip()

        disp = part_headers.get('content-disposition', '')
        name_m     = _re.search(r'name="([^"]*)"',     disp)
        filename_m = _re.search(r'filename="([^"]*)"', disp)
        name = name_m.group(1) if name_m else None
        if name is None:
            continue

        if filename_m:
            files[name] = {
                'filename':     filename_m.group(1),
                'data':         data,
                'content_type': part_headers.get('content-type',
                                                 'application/octet-stream'),
            }
        else:
            fields[name] = data.decode('utf-8', errors='replace')

    return fields, files


# ─── Background job runners ───────────────────────────────────────────────────

def _run_separate(job_dir: Path, input_path: Path):
    """Run Demucs source separation in background thread."""
    out_dir = job_dir / 'demucs'
    out_dir.mkdir(exist_ok=True)
    _write_status(job_dir, 'running', 'separating', 10)

    # -d cpu  — явно CPU, не пытаться использовать CUDA
    # -j 4    — параллельная обработка 4 чанков (ускоряет на многоядерном CPU)
    cmd = [
        sys.executable, '-m', 'demucs',
        '--two-stems=vocals',
        '-n', DEMUCS_MODEL,
        '-d', 'cpu',
        '-j', str(max(1, DEMUCS_THREADS // 2)),
        '-o', str(out_dir),
        str(input_path),
    ]
    try:
        env = os.environ.copy()
        # Ограничиваем число потоков PyTorch/OMP чтобы не перегружать CPU
        # Используем DEMUCS_THREADS (по умолч. 4) — оставляем ядра ОС свободными
        ncpu = str(DEMUCS_THREADS)
        env['OMP_NUM_THREADS']   = ncpu
        env['MKL_NUM_THREADS']   = ncpu
        env['TORCH_NUM_THREADS'] = ncpu
        # MALLOC_TRIM_THRESHOLD: возвращать память ОС после каждого chunk
        env.setdefault('MALLOC_TRIM_THRESHOLD_', '65536')
        # Оборачиваем в nice -n 15 чтобы снизить приоритет процесса
        # и не класть CPU в 100% на время работы Demucs.
        import shutil as _shutil
        if _shutil.which('nice'):
            cmd = ['nice', '-n', '15'] + cmd
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', env=env
        )
        # Track pid for cancellation via DELETE /api/job/:id
        _write_status(job_dir, 'running', 'separating', 20, pid=proc.pid)
        # Demucs does not emit reliable progress; simulate steps.
        # Kill process if it runs longer than DEMUCS_TIMEOUT_SEC (hang guard).
        _write_status(job_dir, 'running', 'separating', 20)
        start = time.time()
        timed_out = False
        while proc.poll() is None:
            elapsed = time.time() - start
            if elapsed > DEMUCS_TIMEOUT_SEC:
                proc.kill()
                timed_out = True
                break
            # Ramp pct 20→85 over first 10 minutes, then hold at 85
            pct = min(85, 20 + int(elapsed / 600 * 65))
            _write_status(job_dir, 'running', 'separating', pct)
            time.sleep(4)
        if timed_out:
            raise RuntimeError(
                f'Demucs timed out after {DEMUCS_TIMEOUT_SEC//60} min. '
                'Try a shorter track or restart Docker.'
            )

        stdout = proc.stdout.read() if proc.stdout else ''
        if proc.returncode != 0:
            raise RuntimeError(f'demucs exited {proc.returncode}: {stdout[-500:]}')

        # Locate output files
        stem_name = input_path.stem
        vocals_path    = out_dir / DEMUCS_MODEL / stem_name / 'vocals.wav'
        no_vocals_path = out_dir / DEMUCS_MODEL / stem_name / 'no_vocals.wav'

        if not vocals_path.exists():
            # Demucs may use a sanitised stem name — search for it
            search_dirs = list((out_dir / DEMUCS_MODEL).glob('*/vocals.wav'))
            if search_dirs:
                vocals_path    = search_dirs[0]
                no_vocals_path = vocals_path.parent / 'no_vocals.wav'
            else:
                raise FileNotFoundError(
                    f'vocals.wav not found in {out_dir / DEMUCS_MODEL}; '
                    f'got: {list((out_dir / DEMUCS_MODEL).rglob("*.wav"))[:5]}'
                )

        _write_status(job_dir, 'done', 'done', 100, files={
            'vocals':    vocals_path.name,
            'no_vocals': no_vocals_path.name,
            # Store relative paths for /api/file lookups
            '_vocals_path':    str(vocals_path),
            '_no_vocals_path': str(no_vocals_path),
        })

    except Exception as e:
        _write_status(job_dir, 'error', 'error', 0, error=str(e))


def _run_to_midi(job_dir: Path, audio_path: Path,
                 onset_threshold: float, min_note_ms: int,
                 min_freq: float = 80.0, max_freq: float = 1200.0):
    """Run Basic Pitch audio→MIDI in background thread."""
    midi_dir = job_dir / 'midi'
    midi_dir.mkdir(exist_ok=True)
    _write_status(job_dir, 'running', 'converting', 10)

    # minimum_frequency / maximum_frequency обрезают диапазон обнаружения:
    #   80 Гц — нижняя граница вокала (мужской бас)
    #   1200 Гц — верхняя граница (обычный женский сопрано + запас)
    #   Это убирает шумовые ноты в тишине и вне вокального диапазона.
    # --melodia-trick улучшает связность мелодии, убирает случайные ноты.
    cmd = [
        'basic-pitch',
        str(midi_dir),
        str(audio_path),
        '--onset-threshold',     str(onset_threshold),
        '--minimum-note-length', str(min_note_ms),
        '--minimum-frequency',   str(min_freq),
        '--maximum-frequency',   str(max_freq),
        '--melodia-trick',
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace'
        )
        _write_status(job_dir, 'running', 'converting', 30)
        start = time.time()
        while proc.poll() is None:
            elapsed = time.time() - start
            pct = min(85, 30 + int(elapsed / 120 * 55))
            _write_status(job_dir, 'running', 'converting', pct)
            time.sleep(2)

        stdout = proc.stdout.read() if proc.stdout else ''
        if proc.returncode != 0:
            raise RuntimeError(f'basic-pitch exited {proc.returncode}: {stdout[-500:]}')

        # Locate .mid output
        mid_files = list(midi_dir.glob('*.mid')) + list(midi_dir.glob('*.midi'))
        if not mid_files:
            raise FileNotFoundError(f'No .mid file found in {midi_dir}; stdout: {stdout[-200:]}')

        mid_file = mid_files[0]
        _write_status(job_dir, 'done', 'done', 100, files={
            'midi': mid_file.name,
            '_midi_path': str(mid_file),
        })

    except Exception as e:
        _write_status(job_dir, 'error', 'error', 0, error=str(e))


def _run_convert(job_dir: Path, input_path: Path,
                 fmt: str, quality: str):
    """Run ffmpeg audio format conversion in background thread."""
    _write_status(job_dir, 'running', 'converting', 10)

    out_name = f'output.{fmt}'
    out_path = job_dir / out_name

    if fmt == 'mp3':
        cmd = [
            FFMPEG_CMD, '-y',
            '-i', str(input_path),
            '-ar', str(CONVERT_SAMPLE_RATE),
            '-ac', str(CONVERT_CHANNELS),
            '-b:a', f'{quality}k',
            str(out_path),
        ]
    else:  # wav
        cmd = [
            FFMPEG_CMD, '-y',
            '-i', str(input_path),
            '-ar', str(CONVERT_SAMPLE_RATE),
            '-ac', str(CONVERT_CHANNELS),
            '-c:a', 'pcm_s16le',
            str(out_path),
        ]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace'
        )
        _write_status(job_dir, 'running', 'converting', 40)
        while proc.poll() is None:
            time.sleep(1)
            _write_status(job_dir, 'running', 'converting', 70)

        stdout = proc.stdout.read() if proc.stdout else ''
        if proc.returncode != 0:
            raise RuntimeError(f'ffmpeg exited {proc.returncode}: {stdout[-500:]}')

        if not out_path.exists():
            raise FileNotFoundError(f'ffmpeg did not produce {out_path}')

        _write_status(job_dir, 'done', 'done', 100, files={
            'converted': out_name,
            '_converted_path': str(out_path),
        })

    except Exception as e:
        _write_status(job_dir, 'error', 'error', 0, error=str(e))


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class ToolsHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Print to stdout so Docker logs show every request
        msg = fmt % args
        print(f'[{self.address_string()}] {msg}', flush=True)

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin',  ALLOW_ORIGIN)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode('utf-8')
        if code >= 400 or 'error' in data:
            print(f'[tools_server] RESPONSE {code}: {json.dumps(data)[:300]}', flush=True)
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, msg: str):
        print(f'[tools_server] ERROR {code}: {msg}', flush=True)
        self._json(code, {'error': msg})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')
        parts  = [p for p in path.split('/') if p]  # e.g. ['api', 'ping']

        # GET /api/ping
        if parts == ['api', 'ping']:
            demucs_ok      = _check_tool([sys.executable, '-m', 'demucs', '--version'])
            basic_pitch_ok = _check_tool(['basic-pitch', '--version'])
            ffmpeg_ok      = _check_tool([FFMPEG_CMD, '-version'])
            self._json(200, {
                'ok':          demucs_ok or basic_pitch_ok,
                'demucs':      demucs_ok,
                'basic_pitch': basic_pitch_ok,
                'ffmpeg':      ffmpeg_ok,
            })
            return

        # GET /api/job/<jobid>
        if len(parts) == 3 and parts[0] == 'api' and parts[1] == 'job':
            jobid = parts[2]
            if not _safe_filename(jobid):
                self._error(400, 'invalid job id')
                return
            job_dir = TMP_BASE / jobid
            if not job_dir.is_dir():
                self._error(404, 'job not found')
                return
            self._json(200, _read_status(job_dir))
            return

        # GET /api/file/<jobid>/<filename>
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'file':
            jobid    = parts[2]
            filename = parts[3]
            if not _safe_filename(jobid) or not _safe_filename(filename):
                self._error(400, 'invalid path')
                return
            job_dir = TMP_BASE / jobid
            if not job_dir.is_dir():
                self._error(404, 'job not found')
                return

            # Search for the file recursively (demucs nests output)
            matches = list(job_dir.rglob(filename))
            if not matches:
                self._error(404, f'file {filename!r} not found in job {jobid}')
                return

            file_path = matches[0]
            data = file_path.read_bytes()

            # Determine content type
            ext = file_path.suffix.lower()
            ct_map = {
                '.wav':  'audio/wav',
                '.mp3':  'audio/mpeg',
                '.mid':  'audio/midi',
                '.midi': 'audio/midi',
            }
            ct = ct_map.get(ext, 'application/octet-stream')

            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition',
                             f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)
            return

        self._error(404, 'not found')

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')
        parts  = [p for p in path.split('/') if p]

        # POST /api/separate
        if parts == ['api', 'separate']:
            print(f'[tools_server] POST /api/separate — content-length={self.headers.get("Content-Length","?")}', flush=True)
            try:
                fields, files = _parse_multipart(self)
            except Exception as e:
                self._error(400, f'multipart parse error: {e}')
                return

            if 'audio' not in files:
                self._error(400, 'missing "audio" field')
                return

            audio = files['audio']
            ext   = Path(audio['filename']).suffix.lower() or '.wav'
            jobid = uuid.uuid4().hex
            job_dir = TMP_BASE / jobid
            job_dir.mkdir(parents=True)

            input_path = job_dir / f'input{ext}'
            input_path.write_bytes(audio['data'])
            _write_status(job_dir, 'running', 'separating', 0)

            t = threading.Thread(
                target=_run_separate,
                args=(job_dir, input_path),
                daemon=True,
            )
            t.start()
            self._json(200, {'job': jobid, 'status': 'started'})
            return

        # POST /api/to-midi
        if parts == ['api', 'to-midi']:
            print(f'[tools_server] POST /api/to-midi — content-length={self.headers.get("Content-Length","?")}', flush=True)
            try:
                fields, files = _parse_multipart(self)
            except Exception as e:
                self._error(400, f'multipart parse error: {e}')
                return

            if 'audio' not in files:
                self._error(400, 'missing "audio" field')
                return

            try:
                onset_threshold = float(fields.get('onset_threshold', '0.65'))
                min_note_ms     = int(fields.get('min_note_ms', '280'))
            except ValueError:
                self._error(400, 'invalid onset_threshold or min_note_ms')
                return

            audio = files['audio']
            jobid = uuid.uuid4().hex
            job_dir = TMP_BASE / jobid
            job_dir.mkdir(parents=True)

            audio_path = job_dir / 'vocals.wav'
            audio_path.write_bytes(audio['data'])
            _write_status(job_dir, 'running', 'converting', 0)

            t = threading.Thread(
                target=_run_to_midi,
                args=(job_dir, audio_path, onset_threshold, min_note_ms,
                      float(fields.get('minimum_frequency', '80')),
                      float(fields.get('maximum_frequency', '1200'))),
                daemon=True,
            )
            t.start()
            self._json(200, {'job': jobid, 'status': 'started'})
            return

        # POST /api/convert
        if parts == ['api', 'convert']:
            print(f'[tools_server] POST /api/convert — content-length={self.headers.get("Content-Length","?")}', flush=True)
            try:
                fields, files = _parse_multipart(self)
            except Exception as e:
                self._error(400, f'multipart parse error: {e}')
                return

            if 'audio' not in files:
                self._error(400, 'missing "audio" field')
                return

            fmt     = fields.get('format', 'wav').lower()
            quality = fields.get('quality', '192')
            if fmt not in ('wav', 'mp3'):
                self._error(400, 'format must be "wav" or "mp3"')
                return

            audio = files['audio']
            ext   = Path(audio['filename']).suffix.lower() or '.webm'
            jobid = uuid.uuid4().hex
            job_dir = TMP_BASE / jobid
            job_dir.mkdir(parents=True)

            input_path = job_dir / f'input{ext}'
            input_path.write_bytes(audio['data'])
            _write_status(job_dir, 'running', 'converting', 0)

            t = threading.Thread(
                target=_run_convert,
                args=(job_dir, input_path, fmt, quality),
                daemon=True,
            )
            t.start()
            self._json(200, {'job': jobid, 'status': 'started'})
            return

        self._error(404, 'not found')

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')
        parts  = [p for p in path.split('/') if p]

        # DELETE /api/job/<jobid>
        if len(parts) == 3 and parts[0] == 'api' and parts[1] == 'job':
            jobid = parts[2]
            if not _safe_filename(jobid):
                self._error(400, 'invalid job id')
                return
            job_dir = TMP_BASE / jobid
            if job_dir.is_dir():
                # Убиваем запущенный процесс если он ещё работает
                try:
                    status_file = job_dir / 'status.json'
                    if status_file.exists():
                        st = json.loads(status_file.read_text())
                        pid = st.get('pid')
                        if pid and st.get('status') == 'running':
                            import signal as _signal
                            try:
                                os.kill(int(pid), _signal.SIGTERM)
                                print(f'[tools_server] Sent SIGTERM to pid {pid} (job {jobid[:8]})', flush=True)
                            except ProcessLookupError:
                                pass  # уже завершился
                except Exception as kill_err:
                    print(f'[tools_server] DELETE kill failed: {kill_err}', flush=True)
                shutil.rmtree(job_dir, ignore_errors=True)
            self._json(200, {'ok': True})
            return

        self._error(404, 'not found')


# ─── Server lifecycle ─────────────────────────────────────────────────────────

def run_server():
    _cleanup_old_jobs()
    _start_cleanup_thread()
    server = ThreadingHTTPServer((LISTEN_HOST, PORT), ToolsHandler)
    print(f'[tools_server] Listening on http://{LISTEN_HOST}:{PORT}')
    print(f'[tools_server] Demucs model : {DEMUCS_MODEL}')
    print(f'[tools_server] ffmpeg cmd   : {FFMPEG_CMD}')
    print(f'[tools_server] tmp dir      : {TMP_BASE}')
    print('[tools_server] Press Ctrl+C to stop.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[tools_server] Shutting down…')
    finally:
        server.server_close()
        # Clean up all job directories on exit
        if TMP_BASE.exists():
            shutil.rmtree(TMP_BASE, ignore_errors=True)
            print('[tools_server] Cleaned up tmp/ directory.')


if __name__ == '__main__':
    run_server()
