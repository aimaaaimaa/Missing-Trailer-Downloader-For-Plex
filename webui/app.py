import os
import re
import sys
import time
import yaml
import threading
import subprocess
import yt_dlp
from datetime import datetime
from flask import Flask, render_template, jsonify, request, Response, stream_with_context

app = Flask(__name__)

# Paths
MTDP_DIR    = os.environ.get('MTDP_DIR', '/app')
CONFIG_PATH = os.environ.get('CONFIG_PATH', '/config/config.yml')
LOGS_BASE   = os.path.join(MTDP_DIR, 'Logs')
MOVIES_SCRIPT = os.path.join(MTDP_DIR, 'Modules', 'Movies.py')
TV_SCRIPT     = os.path.join(MTDP_DIR, 'Modules', 'TV.py')

ANSI_RE = re.compile(r'\033\[[0-9;]*[mKJ]')

def strip_ansi(text):
    return ANSI_RE.sub('', text)

def get_cookies_path():
    path = '/cookies/cookies.txt'
    return path if os.path.isfile(path) else None

# ── Run state ──────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_state = {
    'active': False,
    'process': None,
    'type': None,
    'started_at': None,
    'trigger_time': None,
}

# ── Log helpers ────────────────────────────────────────────────────────────────

def get_log_files():
    """Return all log files sorted newest-first."""
    files = []
    for subdir in ['Movies', 'TV Shows']:
        log_dir = os.path.join(LOGS_BASE, subdir)
        if not os.path.isdir(log_dir):
            continue
        for name in os.listdir(log_dir):
            if name.startswith('log_') and name.endswith('.txt'):
                path = os.path.join(log_dir, name)
                try:
                    mtime = os.path.getmtime(path)
                    size  = os.path.getsize(path)
                except OSError:
                    continue
                files.append({'path': path, 'name': name, 'type': subdir,
                               'mtime': mtime, 'size': size})
    files.sort(key=lambda x: x['mtime'], reverse=True)
    return files


def parse_log(path):
    """Parse a log file and return a stats dict including per-item reasons."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
    except OSError:
        return {}

    lines = [strip_ansi(l) for l in raw.split('\n')]

    result = {
        'downloaded': [], 'missing': [], 'errors': [], 'skipped': [],
        'runtime': None, 'total': 0, 'checked': 0,
        'libraries': [], 'completed': False,
        'reasons': {},  # "Title (Year)" or "Show Title" -> reason string
    }

    section = None
    progress_re = re.compile(r'Checking (?:movie|show) (\d+)/(\d+): (.+)')
    library_re  = re.compile(r'Checking your (.+?) library for missing trailers')

    section_headers = {
        'skipped (Matching Genre):': 'skipped',
        'missing trailers:':         'missing',
        'successfully downloaded trailers:': 'downloaded',
        'failed trailer downloads:': 'errors',
        'refreshing metadata':       None,   # closes the section, items discarded
    }

    # Per-item reason tracking
    current_title = None   # title as printed in progress line
    current_year  = None   # year extracted from "Searching trailer for X (YYYY)..."
    item_lines    = []     # log lines between two progress markers

    def flush_item():
        """Determine reason for current_title from its log lines and store it."""
        if not current_title or not item_lines:
            return
        reason = None
        for ln in item_lines:
            ll = ln.lower()
            if 'no suitable videos found' in ll:
                reason = 'No matching video found'
                break
            if 'title doesn\'t match' in ll or "title doesn't match" in ll:
                reason = 'Title match failed'
                break
            if 'sign in to confirm your age' in ll or 'age-restricted' in ll or 'age restricted' in ll:
                reason = 'Age restricted — needs cookies'
                break
            if ('cookies' in ll and ('authentication' in ll or 'sign in' in ll or 'required' in ll)):
                reason = 'Age restricted — needs cookies'
                break
            if 'download failed' in ll or 'failed to download' in ll:
                reason = 'Download failed'
                break
            if 'timed out' in ll or 'timeout' in ll:
                reason = 'Plex timeout'
                break
            if 'unexpected error' in ll:
                reason = 'Unexpected error'
                break
        if reason:
            # Store under both "Title (Year)" and bare "Title" so either lookup works
            if current_year:
                result['reasons'][f'{current_title} ({current_year})'] = reason
            result['reasons'][current_title] = reason

    for line in lines:
        s = line.strip()
        if not s:
            continue

        m = library_re.search(s)
        if m:
            lib = m.group(1).strip()
            if lib not in result['libraries']:
                result['libraries'].append(lib)
            section = None
            continue

        m = progress_re.match(s)
        if m:
            flush_item()
            n, total = int(m.group(1)), int(m.group(2))
            current_title = m.group(3).strip()
            current_year  = None
            item_lines    = []
            result['checked'] = n
            if total > result['total']:
                result['total'] = total
            section = None
            continue

        # Pick up year from "Searching trailer for Title (YYYY)..."
        if current_title and not section and 'Searching trailer for' in s:
            yr = re.search(r'\((\d{4})\)', s)
            if yr:
                current_year = yr.group(1)

        if 'Run Time:' in s:
            flush_item()
            rt = s.split('Run Time:')[-1].strip()
            if rt:
                result['runtime'] = rt
            result['completed'] = True
            section = None
            continue

        if 'No missing trailers!' in s:
            result['completed'] = True
            section = None
            continue

        # Section header?
        _sentinel = object()
        new_section = _sentinel
        for pattern, sec in section_headers.items():
            if pattern in s.lower():
                new_section = sec
                break
        if new_section is not _sentinel:
            section = new_section
            continue

        # Section item
        if section and s:
            result[section].append(s)
            continue

        # Accumulate per-item lines while scanning (before summary sections)
        if current_title and not section:
            item_lines.append(s)

    # In verbose mode the year is never printed so reasons are stored under bare
    # title only.  Promote bare-title reasons to "Title (Year)" keys so the
    # summary-section lookup (which always uses "Title (Year)") finds them.
    year_re = re.compile(r'^(.+?)\s*\(\d{4}\)$')
    all_items = result['errors'] + result['missing'] + result['downloaded'] + result['skipped']
    for item in all_items:
        if item not in result['reasons']:
            m = year_re.match(item)
            if m:
                bare = m.group(1).strip()
                if bare in result['reasons']:
                    result['reasons'][item] = result['reasons'][bare]

    # Fallback reasons from section membership (when log doesn't contain result lines)
    for title in result['errors']:
        if title not in result['reasons']:
            result['reasons'][title] = 'Download failed'
    for title in result['missing']:
        if title not in result['reasons']:
            result['reasons'][title] = 'No video found'

    return result


# ── API ────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    with _lock:
        return jsonify({
            'running':    _state['active'],
            'type':       _state['type'],
            'started_at': _state['started_at'],
        })


@app.route('/api/runs')
def api_runs():
    files = get_log_files()[:60]
    runs = []
    for f in files:
        stats = parse_log(f['path'])
        runs.append({
            'filename': f['name'],
            'type':     f['type'],
            'date':     datetime.fromtimestamp(f['mtime']).strftime('%Y-%m-%d %H:%M'),
            'mtime':    f['mtime'],
            'stats':    stats,
        })
    return jsonify(runs)


@app.route('/api/runs/latest')
def api_runs_latest():
    """Return the latest log for each type (Movies + TV Shows) separately."""
    files = get_log_files()
    result = {}
    for f in files:
        if f['type'] not in result:
            result[f['type']] = {
                'filename': f['name'],
                'type':     f['type'],
                'date':     datetime.fromtimestamp(f['mtime']).strftime('%Y-%m-%d %H:%M'),
                'mtime':    f['mtime'],
                'stats':    parse_log(f['path']),
            }
        if len(result) == 2:
            break
    return jsonify(result if result else None)


@app.route('/api/log')
def api_log_content():
    log_type = request.args.get('type', '')
    filename  = request.args.get('file', '')
    if log_type not in ('Movies', 'TV Shows'):
        return jsonify({'error': 'Invalid type'}), 400
    if '/' in filename or '..' in filename or not filename.endswith('.txt'):
        return jsonify({'error': 'Invalid filename'}), 400
    path = os.path.join(LOGS_BASE, log_type, filename)
    if not os.path.isfile(path):
        return jsonify({'error': 'Not found'}), 404
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        content = strip_ansi(fh.read())
    return jsonify({'content': content})


@app.route('/api/log/stream')
def api_log_stream():
    """SSE: tail the newest log file created at or after `since`."""
    since = float(request.args.get('since', 0))

    def generate():
        # Wait up to 15 s for a log file matching `since`
        path     = None
        deadline = time.time() + 15
        while time.time() < deadline:
            for f in get_log_files():
                if f['mtime'] >= since - 2:
                    path = f['path']
                    break
            if path:
                break
            yield ': waiting\n\n'
            time.sleep(1)

        if not path:
            yield 'data: [No log file found]\n\n'
            return

        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                while True:
                    line = fh.readline()
                    if line:
                        clean = strip_ansi(line.rstrip())
                        if clean:
                            yield f'data: {clean}\n\n'
                    else:
                        with _lock:
                            still_running = _state['active']
                        if not still_running:
                            yield 'event: done\ndata: complete\n\n'
                            return
                        time.sleep(0.3)
        except Exception as e:
            yield f'data: [Stream error: {e}]\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/trigger', methods=['POST'])
def api_trigger():
    data      = request.json or {}
    run_type  = str(data.get('type', '3'))
    full_scan = bool(data.get('full_scan', False))
    if run_type not in ('1', '2', '3'):
        return jsonify({'error': 'Invalid type'}), 400

    with _lock:
        if _state['active']:
            return jsonify({'error': 'A run is already in progress'}), 409

    trigger_time = time.time()
    type_labels  = {'1': 'Movies', '2': 'TV Shows', '3': 'Both'}

    def do_run():
        scripts = []
        if run_type in ('1', '3'):
            scripts.append(MOVIES_SCRIPT)
        if run_type in ('2', '3'):
            scripts.append(TV_SCRIPT)

        env = os.environ.copy()
        env['IS_DOCKER']        = 'true'
        env['PYTHONUNBUFFERED'] = '1'

        label = type_labels[run_type]
        if full_scan:
            label += ' — Full Scan'

        with _lock:
            _state['active']       = True
            _state['type']         = label
            _state['started_at']   = datetime.now().isoformat()
            _state['trigger_time'] = trigger_time

        # Full scan: temporarily override USE_LABELS=false in config so all
        # movies are checked regardless of MTDfP label state.
        original_config = None
        if full_scan:
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    original_config = f.read()
                cfg = yaml.safe_load(original_config) or {}
                cfg['USE_LABELS'] = False
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
                print('Full scan: USE_LABELS temporarily set to false', flush=True)
            except Exception as e:
                print(f'Full scan config override failed: {e}', flush=True)
                original_config = None  # don't try to restore if we never wrote

        try:
            for script in scripts:
                if not os.path.isfile(script):
                    print(f'Script not found: {script}', flush=True)
                    continue
                proc = subprocess.Popen(
                    [sys.executable, script],
                    env=env,
                    cwd=MTDP_DIR,
                )
                with _lock:
                    _state['process'] = proc
                proc.wait()
        except Exception as e:
            print(f'Run error: {e}', flush=True)
        finally:
            if original_config is not None:
                try:
                    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                        f.write(original_config)
                    print('Full scan: config restored', flush=True)
                except Exception as e:
                    print(f'Failed to restore config after full scan: {e}', flush=True)
            with _lock:
                _state['active']  = False
                _state['process'] = None

    threading.Thread(target=do_run, daemon=True).start()
    return jsonify({'status': 'started', 'type': run_type, 'trigger_time': trigger_time})


@app.route('/api/trigger/stop', methods=['POST'])
def api_trigger_stop():
    with _lock:
        if not _state['active']:
            return jsonify({'error': 'No run in progress'}), 409
        proc = _state['process']
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    return jsonify({'status': 'stopping'})


@app.route('/api/config', methods=['GET'])
def api_config_get():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return jsonify({'content': f.read()})
    except OSError as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['POST'])
def api_config_save():
    content = (request.json or {}).get('content', '')
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        return jsonify({'error': f'Invalid YAML: {e}'}), 400
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'status': 'saved'})
    except OSError as e:
        return jsonify({'error': str(e)}), 500


LIB_ROOTS = {
    'Movies':   '/share/Multimedia/Video/Movies',
    'TV Shows': '/share/CE_CACHEDEV1_DATA/Multimedia/Video/TV Shows',
}

def _norm(s):
    s = re.sub(r'\s*[:\-]\s*', ' ', s)
    s = re.sub(r"['\u2019]", '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.lower().strip()

def find_media_folder(title, year, media_type):
    """Return the resolved folder path or None."""
    lib_root    = LIB_ROOTS[media_type]
    search_name = f"{title} ({year})" if year else title
    norm_title  = _norm(title)
    norm_search = _norm(search_name)
    try:
        entries = [e for e in os.scandir(lib_root) if e.is_dir()]
    except OSError:
        return None
    for entry in entries:
        if _norm(entry.name) == norm_search:
            return entry.path
    for entry in entries:
        if norm_title in _norm(entry.name) and (not year or year in entry.name):
            return entry.path
    for entry in entries:
        if norm_title in _norm(entry.name):
            return entry.path
    return None



@app.route('/api/download/manual', methods=['POST'])
def api_manual_download():
    data       = request.json or {}
    title      = data.get('title', '').strip()
    year       = str(data.get('year', '')).strip()
    url        = data.get('url', '').strip()
    media_type = data.get('media_type', 'Movies')

    if not title or not url:
        return jsonify({'error': 'title and url are required'}), 400
    if not url.startswith('http://') and not url.startswith('https://'):
        return jsonify({'error': 'URL must start with http:// or https://'}), 400
    if media_type not in LIB_ROOTS:
        return jsonify({'error': 'Invalid media_type'}), 400

    media_folder = find_media_folder(title, year, media_type)

    if not media_folder:
        return jsonify({'error': f'Folder not found for "{title} ({year})"'}), 404

    trailers_folder = os.path.join(media_folder, 'Trailers')
    os.makedirs(trailers_folder, exist_ok=True)
    sanitized = title.replace(':', ' -')
    out_name  = f"{sanitized} ({year})-trailer" if year else f"{sanitized}-trailer"
    out_tmpl  = os.path.join(trailers_folder, out_name + '.%(ext)s')

    cookies_path = get_cookies_path()
    is_youtube = 'youtube.com' in url or 'youtu.be' in url
    ydl_opts = {
        'outtmpl':    out_tmpl,
        'quiet':      True,
        'no_warnings': True,
        'noplaylist': True,
        'format':     'bestvideo+bestaudio/best',
    }
    if is_youtube:
        ydl_opts['extractor_args'] = {'youtube': {'player_client': ['android']}}
    if cookies_path:
        ydl_opts['cookiefile'] = cookies_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return jsonify({'status': 'downloaded', 'folder': media_folder})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7879, debug=False, threaded=True)
