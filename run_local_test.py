#!/usr/bin/env python3
"""StreamWatcher - local web UI for validating IPTV streams.

Run without arguments to open the setup UI in your browser:

    python run_local_test.py

Optionally pass an M3U URL or local file to pre-fill the playlist field:

    python run_local_test.py data/test_sample.m3u
    python run_local_test.py https://example.com/playlist.m3u

The UI lets you choose channels, duration, loop mode and which stream
properties to validate (resolution, framerate, audio, buffering, errors).
Results are written to ``results.json`` and shown on the live dashboard.
"""

import argparse
import asyncio
import json
import re
import os
import socket
import shutil
import threading
import time
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from src.iptv_monitor.config import ensure_dirs
from src.iptv_monitor.db import add_channels_bulk, init_db
from src.iptv_monitor.worker import (
    fetch_text,
    format_fps,
    monitor_stream,
    parse_m3u_items,
    probe_stream,
    resolution_label,
)

ROOT_DIR = Path(__file__).resolve().parent
RESULTS_PATH = ROOT_DIR / 'results.json'
DEFAULT_PORT = int(os.environ.get('IPTV_PORT', '9001'))
MAX_DURATION = 7 * 24 * 60 * 60


def _write_json(path, payload):
    """Write JSON atomically so the dashboard never reads a partial file."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


def _dedupe_channels(items):
    seen = set()
    result = []
    for item in items:
        url = (item.get('url') or '').strip()
        if not url or url in seen:
            continue
        seen.add(url)
        result.append({
            'name': (item.get('name') or url).strip(),
            'url': url,
            'group': (item.get('group') or 'Ungrouped').strip() or 'Ungrouped',
        })
    return result


def _normalise_options(raw=None):
    raw = raw or {}

    def as_bool(key, default=True):
        return bool(raw.get(key, default))

    def as_int(key, default, low, high):
        try:
            value = int(float(raw.get(key, default)))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    min_height = as_int('min_height', 0, 0, 4320)
    if min_height not in (0, 480, 576, 720, 1080, 1440, 2160):
        min_height = 0

    try:
        min_fps = float(raw.get('min_fps', 0) or 0)
    except (TypeError, ValueError):
        min_fps = 0
    min_fps = max(0.0, min(240.0, min_fps))

    return {
        'check_resolution': as_bool('check_resolution', True),
        'check_framerate': as_bool('check_framerate', True),
        'check_smoothness': as_bool('check_smoothness', True),
        'check_picture': as_bool('check_picture', True),
        'auto_retry': as_bool('auto_retry', False),
        'retry_count': as_int('retry_count', 2, 0, 5),
        'retry_on_5xx': as_bool('retry_on_5xx', True),
        'retry_on_4xx': as_bool('retry_on_4xx', False),
        'check_audio': as_bool('check_audio', True),
        'check_buffering': as_bool('check_buffering', True),
        'check_disconnects': as_bool('check_disconnects', True),
        'min_height': min_height,
        'min_fps': min_fps,
        'probe_timeout': as_int('probe_timeout', 20, 3, 120),
        'concurrency': as_int('concurrency', 1, 1, 10),
    }


async def load_playlist(source=None, content=None):
    """Load and parse a playlist from text, a local file or a URL."""
    if content is not None:
        text = content
    elif source:
        candidate = os.path.expanduser(str(source).strip())
        if not os.path.isfile(candidate) and not os.path.isabs(candidate):
            root_candidate = ROOT_DIR / candidate
            if root_candidate.is_file():
                candidate = str(root_candidate)
        if os.path.isfile(candidate):
            with open(candidate, 'r', encoding='utf-8', errors='replace') as handle:
                text = handle.read()
        else:
            async with aiohttp.ClientSession() as session:
                text = await fetch_text(session, candidate, timeout=300)
    else:
        raise ValueError('Provide a playlist URL, local path or file contents')

    items = parse_m3u_items(text)
    if not items:
        raise ValueError('No channels were found in that playlist')
    return _dedupe_channels(items)


def _parse_xtream_source(source):
    parsed = urlparse(str(source or '').strip())
    query = parse_qs(parsed.query)
    username = (query.get('username') or [None])[0]
    password = (query.get('password') or [None])[0]
    if not username or not password or not parsed.scheme or not parsed.netloc:
        return None
    return f'{parsed.scheme}://{parsed.netloc}', str(username), str(password)


async def xtream_categories(source):
    info = _parse_xtream_source(source)
    if not info:
        raise ValueError('Not an Xtream Codes URL (missing username/password)')
    base, username, password = info
    url = (
        f'{base}/player_api.php?username={quote(username, safe="")}'
        f'&password={quote(password, safe="")}&action=get_live_categories'
    )
    async with aiohttp.ClientSession() as session:
        text = await fetch_text(session, url, timeout=60)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError('Could not parse Xtream categories response')
    if not isinstance(data, list):
        raise ValueError('Unexpected Xtream categories response')
    categories = []
    for item in data:
        if not isinstance(item, dict):
            continue
        categories.append({
            'id': str(item.get('category_id', '')),
            'name': str(item.get('category_name') or item.get('name') or 'Unknown').strip() or 'Unknown',
        })
    categories.sort(key=lambda item: item['name'].lower())
    return categories


async def xtream_streams(source, category_ids):
    info = _parse_xtream_source(source)
    if not info:
        raise ValueError('Not an Xtream Codes URL (missing username/password)')
    base, username, password = info
    eu = quote(username, safe='')
    ep = quote(password, safe='')
    try:
        all_categories = await xtream_categories(source)
    except Exception:
        all_categories = []
    names = {item['id']: item['name'] for item in all_categories}

    channels = []
    for raw_id in category_ids or []:
        category_id = str(raw_id)
        url = (
            f'{base}/player_api.php?username={eu}&password={ep}'
            f'&action=get_live_streams&category_id={quote(category_id, safe="")}'
        )
        async with aiohttp.ClientSession() as session:
            text = await fetch_text(session, url, timeout=120)
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            raise ValueError(f'Could not parse streams for category {category_id}')
        if not isinstance(data, list):
            continue
        group_name = names.get(category_id, f'Category {category_id}')
        for item in data:
            if not isinstance(item, dict):
                continue
            stream_id = item.get('stream_id')
            if stream_id is None:
                continue
            name = str(item.get('name') or item.get('title') or f'Stream {stream_id}').strip()
            stream_url = f'{base}/live/{username}/{password}/{stream_id}.ts'
            channels.append({'name': name, 'url': stream_url, 'group': group_name})

    return _dedupe_channels(channels)


def _new_channel_entry(name, url, group='', previous=None):
    previous = previous or {}
    return {
        'name': name,
        'url': url,
        'group': group or 'Ungrouped',
        'status': 'pending',
        'details': '',
        'resolution': '',
        'resolution_label': '',
        'framerate': '',
        'fps': None,
        'video_codec': '',
        'audio_codec': '',
        'interlaced': False,
        'field_order': '',
        'smoothness': None,
        'smooth': None,
        'smooth_ok': None,
        'black_screen': False,
        'black_frames': 0,
        'bitrate_kbps': None,
        'test_duration': '',
        'tested_seconds': 0,
        'issues': {'buffering': 0, 'errors': 0},
        'frame_drops': 0,
        'disconnects_count': previous.get('disconnects_count', 0),
        'disconnects': list(previous.get('disconnects', [])),
        'buffering_total_seconds': previous.get('buffering_total_seconds', 0.0),
        'buffering_events': list(previous.get('buffering_events', [])),
        'resolution_ok': None,
        'framerate_ok': None,
    }


def _snapshot(channels_json, test_start, duration_seconds, loop_mode,
              current_iteration, options, run_id, extra=None):
    payload = {
        'channels': channels_json,
        'test_start': test_start,
        'test_duration': duration_seconds,
        'loop_mode': loop_mode,
        'current_iteration': current_iteration,
        'run_id': run_id,
        'options': options,
        'state': 'running',
        'generated_at': int(time.time()),
    }
    if extra:
        payload.update(extra)
    _write_json(RESULTS_PATH, payload)


async def run_checks_async(channels, duration_seconds=60, check_interval=1,
                           loop_mode='single', current_iteration=0,
                           channels_json=None, options=None, stop_event=None,
                           run_id=None):
    """Probe every channel and keep results.json up to date as we go."""
    options = _normalise_options(options)
    test_start = int(time.time())
    lock = asyncio.Lock()

    def channel_parts(channel):
        if len(channel) >= 4:
            return channel[0], channel[1], channel[2], channel[3]
        return channel[0], channel[1], channel[2], ''

    if channels_json is None:
        channels_json = [
            _new_channel_entry(channel_parts(channel)[1], channel_parts(channel)[2], channel_parts(channel)[3])
            for channel in channels
        ]

    def write_snapshot(extra=None):
        _snapshot(
            channels_json, test_start, duration_seconds, loop_mode,
            current_iteration, options, run_id, extra=extra,
        )

    write_snapshot({'state': 'running'})

    def add_note(notes, text):
        text = (text or '').strip()
        if text and text not in notes:
            notes.append(text)

    async def probe_channel(idx, channel):
        cid, name, url, group = channel_parts(channel)
        state = channels_json[idx]
        channel_start = time.time()
        notes = []
        buffering_count = 0
        error_count = 0
        drop_count = 0
        stopped = False

        if stop_event is not None and stop_event.is_set():
            async with lock:
                state['status'] = 'stopped'
                state['tested_seconds'] = 0
                write_snapshot({'state': 'stopping'})
            return {
                'id': cid, 'name': name, 'url': url,
                'result': 'stopped', 'notes': 'Stopped before testing',
            }

        async with lock:
            state['status'] = 'testing'
            write_snapshot()

        try:
            attempts = 1
            if options.get('auto_retry'):
                attempts += max(0, int(options.get('retry_count', 0)))
            info = None
            for attempt in range(attempts):
                if stop_event is not None and stop_event.is_set():
                    break
                info = await probe_stream(
                    url, per_check_timeout=options['probe_timeout'],
                    check_smoothness=options.get('check_smoothness', False),
                    check_picture=options.get('check_picture', True),
                    stop_event=stop_event,
                )
                if info.get('ok') or (stop_event is not None and stop_event.is_set()):
                    break

                # Decide whether this failure is retryable based on the
                # selected HTTP status code classes.
                notes_text = (info.get('notes') or '').lower()
                http_codes = re.findall(r'\\b([45]\\d{2})\\b', notes_text)
                has_5xx = any(code.startswith('5') for code in http_codes)
                has_4xx = any(code.startswith('4') for code in http_codes)

                retryable = False
                if options.get('auto_retry'):
                    specific = options.get('retry_on_4xx') or options.get('retry_on_5xx')
                    if not specific:
                        retryable = True
                    else:
                        if options.get('retry_on_5xx') and has_5xx:
                            retryable = True
                        if options.get('retry_on_4xx') and has_4xx:
                            retryable = True
                        # If the failure is a generic probe failure (no HTTP
                        # code) and auto-retry is on, retry it as well.
                        if not http_codes:
                            retryable = True
                if not retryable or attempt >= attempts - 1:
                    break
                add_note(notes, f'Probe attempt {attempt + 1} failed; retrying…')
                # Sleep in small steps so stop requests stay responsive.
                for _ in range(10):
                    if stop_event is not None and stop_event.is_set():
                        break
                    await asyncio.sleep(0.1)
            if info is None:
                info = {'ok': False, 'notes': 'Probe did not run', 'stopped': False}
            problems = []

            async with lock:
                state.update({
                    'resolution': info.get('resolution') or '',
                    'resolution_label': info.get('resolution_label') or '',
                    'framerate': info.get('framerate') or '',
                    'fps': info.get('fps'),
                    'video_codec': info.get('video_codec') or '',
                    'audio_codec': info.get('audio_codec') or '',
                    'bitrate_kbps': info.get('bitrate_kbps'),
                    'interlaced': bool(info.get('interlaced')),
                    'field_order': info.get('field_order') or '',
                    'smoothness': info.get('smoothness'),
                    'smooth': (info.get('smoothness') or {}).get('smooth'),
                    'smooth_ok': (info.get('smoothness') or {}).get('smooth'),
                    'black_screen': bool(info.get('black_screen')),
                    'black_frames': info.get('black_frames', 0),
                })
                if info.get('notes'):
                    add_note(notes, info['notes'])

                if options['check_resolution']:
                    height = info.get('height') or 0
                    if not info.get('resolution'):
                        state['resolution_ok'] = False
                        problems.append('No video resolution detected')
                    elif options['min_height'] and height < options['min_height']:
                        state['resolution_ok'] = False
                        problems.append(
                            f'Resolution {info.get("resolution_label") or info.get("resolution")} '
                            f'is below {resolution_label(None, options["min_height"])}'
                        )
                    else:
                        state['resolution_ok'] = True

                if options['check_framerate']:
                    fps = info.get('fps')
                    if not fps:
                        state['framerate_ok'] = False
                        problems.append('Framerate was not reported by the stream')
                    elif options['min_fps'] and fps + 0.25 < options['min_fps']:
                        state['framerate_ok'] = False
                        problems.append(
                            f'Framerate {format_fps(fps)} is below {format_fps(options["min_fps"])}'
                        )
                    else:
                        state['framerate_ok'] = True

                if options['check_smoothness']:
                    smooth = (info.get('smoothness') or {}).get('smooth')
                    if smooth is False:
                        state['smooth_ok'] = False
                        smooth_notes = (info.get('smoothness') or {}).get('notes') or 'Stream is not smooth'
                        problems.append(smooth_notes)
                    elif smooth is True:
                        state['smooth_ok'] = True

                if options['check_audio'] and not info.get('audio_codec'):
                    problems.append('No audio stream detected')

                write_snapshot()

            if info.get('stopped'):
                stopped = True

            monitor_enabled = options['check_buffering'] or options['check_disconnects']
            monitor_stats = None

            if info['ok'] and monitor_enabled and not (stop_event is not None and stop_event.is_set()):
                async def update_progress():
                    while True:
                        try:
                            await asyncio.sleep(1)
                            async with lock:
                                state['tested_seconds'] = int(time.time() - channel_start)
                                write_snapshot()
                        except asyncio.CancelledError:
                            break
                        except Exception:
                            break

                progress_task = asyncio.create_task(update_progress())
                try:
                    monitor_stats = await monitor_stream(url, duration_seconds, stop_event=stop_event)
                finally:
                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass

                buffering_count = int(monitor_stats.get('buffering', 0))
                error_count = int(monitor_stats.get('errors', 0))
                drop_count = int(monitor_stats.get('drops', 0))
                stopped = bool(monitor_stats.get('stopped'))
                if options['check_disconnects']:
                    add_note(notes, monitor_stats.get('notes', ''))
                if options['check_buffering'] and monitor_stats.get('buffering_events'):
                    state['buffering_events'].extend(monitor_stats['buffering_events'])
                else:
                    buffering_count = 0
                if options['check_disconnects'] and error_count:
                    state['disconnects_count'] = int(state.get('disconnects_count', 0)) + error_count
                    now = int(time.time())
                    state['disconnects'].extend([now] * error_count)
                elif not options['check_disconnects']:
                    error_count = 0
                if drop_count:
                    state['frame_drops'] = int(state.get('frame_drops', 0)) + drop_count
                    add_note(notes, f'{drop_count} dropped frame{"s" if drop_count != 1 else ""} during monitoring')
                if monitor_stats.get('elapsed'):
                    state['test_duration'] = f"{monitor_stats['elapsed']:.2f}s"

            elif info['ok']:
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                # Metadata-only test: keep the row marked as tested briefly.
                state['test_duration'] = f"{info.get('elapsed', 0):.2f}s"

            if not info['ok']:
                status = 'stopped' if stopped else 'error'
            elif stopped:
                status = 'stopped'
            elif problems or buffering_count or error_count:
                status = 'issue'
            else:
                status = 'pass'

            async with lock:
                state['status'] = status
                state['details'] = ' • '.join(dict.fromkeys([n for n in notes if n]))[:800]
                state['issues'] = {
                    'buffering': buffering_count,
                    'errors': error_count,
                    'drops': drop_count,
                }
                state['tested_seconds'] = int(time.time() - channel_start)
                write_snapshot({'state': 'stopping' if stopped else 'running'})
        except Exception as exc:
            async with lock:
                state['status'] = 'error'
                state['details'] = str(exc)[:800]
                state['tested_seconds'] = int(time.time() - channel_start)
                write_snapshot()

        return {
            'id': cid,
            'name': name,
            'url': url,
            'result': state['status'],
            'notes': state['details'],
            'resolution': state.get('resolution', ''),
            'framerate': state.get('framerate', ''),
        }

    async def limited_probe(idx, channel):
        async with semaphore:
            return await probe_channel(idx, channel)

    semaphore = asyncio.Semaphore(options['concurrency'])
    tasks = [
        asyncio.create_task(limited_probe(idx, channel))
        for idx, channel in enumerate(channels)
    ]
    results = await asyncio.gather(*tasks)

    final_state = 'stopped' if (stop_event is not None and stop_event.is_set()) else 'finished'
    write_snapshot({'state': final_state})
    return results


async def prepare_and_run(source, duration_seconds=60, loop_mode='single',
                          current_iteration=0, selected_channels=None,
                          options=None, stop_event=None, run_id=None):
    ensure_dirs()
    await init_db()
    options = _normalise_options(options)

    if selected_channels:
        items = _dedupe_channels(selected_channels)
    else:
        selected_urls = None
        for selection_file in (Path.cwd() / 'channel_selection.json', ROOT_DIR / 'channel_selection.json'):
            if selection_file.is_file():
                try:
                    with open(selection_file, 'r', encoding='utf-8') as handle:
                        selected = json.load(handle)
                    selected_urls = {item['url'] for item in selected if item.get('url')}
                except Exception as exc:
                    print(f'[WARN] Could not read {selection_file}: {exc}')
                break

        items = await load_playlist(source)
        if selected_urls is not None:
            items = [item for item in items if item['url'] in selected_urls]

    items = _dedupe_channels(items)
    if not items:
        raise ValueError('No channels to test')

    ids = await add_channels_bulk([(item['name'], item['url']) for item in items])
    channels_to_check = [
        (channel_id, item['name'], item['url'], item['group'])
        for channel_id, item in zip(ids, items)
    ]

    previous_counts = {}
    if current_iteration > 1 and RESULTS_PATH.exists():
        try:
            with open(RESULTS_PATH, 'r', encoding='utf-8') as handle:
                prev_data = json.load(handle)
            for channel in prev_data.get('channels', []):
                previous_counts[channel.get('url')] = {
                    'buffering_events': channel.get('buffering_events', []),
                    'disconnects': channel.get('disconnects', []),
                    'disconnects_count': channel.get('disconnects_count', 0),
                    'buffering_total_seconds': channel.get('buffering_total_seconds', 0.0),
                }
        except Exception:
            pass

    channels_json = [
        _new_channel_entry(
            name, url, group,
            previous=previous_counts.get(url),
        )
        for _cid, name, url, group in channels_to_check
    ]

    test_start = int(time.time())
    _snapshot(
        channels_json, test_start, duration_seconds, loop_mode,
        current_iteration, options, run_id, extra={'state': 'running'},
    )

    print(f'Starting {len(channels_to_check)} channel checks (duration={duration_seconds}s)...')
    results = await run_checks_async(
        channels_to_check,
        duration_seconds=duration_seconds,
        loop_mode=loop_mode,
        current_iteration=current_iteration,
        channels_json=channels_json,
        options=options,
        stop_event=stop_event,
        run_id=run_id,
    )

    print('\nResults:')
    for result in results:
        print(
            f"{result['name']}: {result['result']} - {result.get('notes', '')} "
            f"[{result.get('resolution', '') or 'no resolution'}, "
            f"{result.get('framerate', '') or 'no framerate'}]"
        )
    return results


class TestManager:
    """Runs the async test loop in a background thread for the web UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._state = 'idle'
        self._error = None
        self._config = None
        self._run_id = None
        self._started_at = None
        self._finished_at = None

    def start(self, config):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, 'A test is already running'

            self._stop_event.clear()
            self._run_id = uuid.uuid4().hex[:12]
            self._config = config
            self._state = 'running'
            self._error = None
            self._started_at = int(time.time())
            self._finished_at = None

            # Write an initial pending snapshot so the dashboard has something
            # to show immediately after the browser navigates to it.
            channels_json = [
                _new_channel_entry(item['name'], item['url'], item.get('group', ''))
                for item in config['selected_channels']
            ]
            try:
                _snapshot(
                    channels_json, self._started_at, config['duration_seconds'],
                    config['loop_mode'], 1, config['options'], self._run_id,
                    extra={'state': 'starting'},
                )
            except Exception:
                pass

            self._thread = threading.Thread(
                target=self._run, args=(config, self._run_id), daemon=True
            )
            self._thread.start()
            return True, 'Test started'

    def stop(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False, 'No test is running'
            self._state = 'stopping'
            self._stop_event.set()
            return True, 'Stopping test...'

    def status(self):
        with self._lock:
            config = self._config or {}
            return {
                'state': self._state,
                'running': self._thread is not None and self._thread.is_alive(),
                'error': self._error,
                'run_id': self._run_id,
                'started_at': self._started_at,
                'finished_at': self._finished_at,
                'source': config.get('source'),
                'channel_count': len(config.get('selected_channels', [])),
                'duration_seconds': config.get('duration_seconds'),
                'loop_mode': config.get('loop_mode'),
                'loop_iterations': config.get('loop_iterations'),
            }

    def _run(self, config, run_id):
        try:
            loop_mode = config['loop_mode']
            loop_iterations = config['loop_iterations']
            iteration = 0
            while not self._stop_event.is_set():
                iteration += 1
                if loop_mode == 'single' and iteration > 1:
                    break
                if loop_mode == 'loop-times' and iteration > loop_iterations:
                    break

                if loop_mode != 'single':
                    print(f'[INFO] Iteration {iteration} starting...')

                asyncio.run(prepare_and_run(
                    config.get('source') or 'selected channels',
                    duration_seconds=config['duration_seconds'],
                    loop_mode=loop_mode,
                    current_iteration=iteration,
                    selected_channels=config['selected_channels'],
                    options=config['options'],
                    stop_event=self._stop_event,
                    run_id=run_id,
                ))

                if loop_mode == 'single':
                    break
                if self._stop_event.is_set():
                    break
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
                self._state = 'error'
            print(f'[ERROR] {exc}')
        finally:
            with self._lock:
                self._finished_at = int(time.time())
                if self._state != 'error':
                    self._state = 'idle'
                self._thread = None


class AppHandler(SimpleHTTPRequestHandler):
    manager = None
    default_source = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 20 * 1024 * 1024:
            raise ValueError('Request body is too large')
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise ValueError('Invalid JSON request body')

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/defaults':
            self._send_json({
                'default_source': self.default_source or '',
                'port': self.server.server_port,
                'ffprobe': shutil.which('ffprobe') is not None,
                'ffmpeg': shutil.which('ffmpeg') is not None,
                'version': '1.0',
            })
            return
        if parsed.path == '/api/status':
            self._send_json(self.manager.status())
            return
        if parsed.path == '/api/results':
            try:
                with open(RESULTS_PATH, 'r', encoding='utf-8') as handle:
                    self._send_json(json.load(handle))
            except FileNotFoundError:
                self._send_json({'channels': [], 'state': 'idle'}, status=404)
            except Exception as exc:
                self._send_json({'error': str(exc)}, status=500)
            return
        if parsed.path == '/':
            self.path = '/index.html'
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send_json({'ok': False, 'error': str(exc)}, status=400)
            return

        if parsed.path == '/api/load':
            try:
                items = asyncio.run(load_playlist(
                    source=body.get('source'),
                    content=body.get('content'),
                ))
                groups = sorted({item['group'] for item in items}, key=str.lower)
                self._send_json({
                    'ok': True,
                    'channels': items,
                    'groups': groups,
                    'count': len(items),
                })
            except Exception as exc:
                self._send_json({'ok': False, 'error': str(exc)}, status=400)
            return

        if parsed.path == '/api/xtream/categories':
            try:
                categories = asyncio.run(xtream_categories(body.get('source')))
                self._send_json({
                    'ok': True,
                    'categories': categories,
                    'count': len(categories),
                })
            except Exception as exc:
                self._send_json({'ok': False, 'error': str(exc)}, status=400)
            return

        if parsed.path == '/api/xtream/streams':
            raw_ids = body.get('category_ids') or []
            if not isinstance(raw_ids, list):
                self._send_json({'ok': False, 'error': 'category_ids must be a list'}, status=400)
                return
            try:
                channels = asyncio.run(xtream_streams(body.get('source'), raw_ids))
                groups = sorted({item['group'] for item in channels}, key=str.lower)
                self._send_json({
                    'ok': True,
                    'channels': channels,
                    'groups': groups,
                    'count': len(channels),
                })
            except Exception as exc:
                self._send_json({'ok': False, 'error': str(exc)}, status=400)
            return

        if parsed.path == '/api/start':
            selected = body.get('selected_channels') or []
            if not isinstance(selected, list):
                self._send_json({'ok': False, 'error': 'selected_channels must be a list'}, status=400)
                return
            cleaned = _dedupe_channels(selected)
            if not cleaned:
                self._send_json({'ok': False, 'error': 'Select at least one channel'}, status=400)
                return

            try:
                duration = int(float(body.get('duration_seconds', 60)))
            except (TypeError, ValueError):
                duration = 60
            duration = max(1, min(MAX_DURATION, duration))

            loop_mode = body.get('loop_mode', 'single')
            if loop_mode not in ('single', 'loop-times', 'infinite'):
                loop_mode = 'single'
            try:
                loop_iterations = int(float(body.get('loop_iterations', 1)))
            except (TypeError, ValueError):
                loop_iterations = 1
            loop_iterations = max(1, min(10000, loop_iterations))

            config = {
                'source': str(body.get('source') or 'selected channels')[:2000],
                'selected_channels': cleaned,
                'duration_seconds': duration,
                'loop_mode': loop_mode,
                'loop_iterations': loop_iterations,
                'options': _normalise_options(body.get('options')),
            }
            started, message = self.manager.start(config)
            if not started:
                self._send_json({'ok': False, 'error': message}, status=409)
                return
            self._send_json({
                'ok': True,
                'message': message,
                'run_id': self.manager.status().get('run_id'),
            })
            return

        if parsed.path == '/api/stop':
            stopped, message = self.manager.stop()
            self._send_json({'ok': stopped, 'message': message}, status=200 if stopped else 409)
            return

        self._send_json({'ok': False, 'error': 'Unknown endpoint'}, status=404)

    def log_message(self, fmt, *args):
        # Keep the terminal readable while the dashboard polls every second.
        message = fmt % args if args else fmt
        if '/api/status' in message or '/api/results' in message or 'results.json' in message:
            return
        super().log_message(fmt, *args)


def _port_is_free(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(('', port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _choose_port(preferred, attempts=20):
    for port in range(preferred, preferred + attempts):
        if _port_is_free(port):
            return port
    return preferred


def start_http_server(port=DEFAULT_PORT, no_browser=False, open_path='/'):
    chosen_port = _choose_port(port)
    server = ThreadingHTTPServer(('', chosen_port), AppHandler)
    server.daemon_threads = True
    url = f'http://localhost:{chosen_port}{open_path}'
    print(f'[INFO] StreamWatcher UI: {url}')
    print('[INFO] Press Ctrl+C to stop the server.')
    if not no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[INFO] HTTP server stopped.')
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description='StreamWatcher IPTV stream validator')
    parser.add_argument('source', nargs='?', help='Optional M3U URL or local file to pre-fill')
    parser.add_argument('duration', nargs='?', type=int, help='Optional test duration in seconds')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='HTTP port (default: 9001)')
    parser.add_argument('--no-browser', action='store_true', help='Do not open a browser window')
    args = parser.parse_args()

    source = args.source or os.environ.get('IPTV_SOURCE', '')
    ensure_dirs()

    manager = TestManager()
    AppHandler.manager = manager
    AppHandler.default_source = source

    params = []
    if source:
        params.append('source=' + quote(source, safe=''))
    if args.duration:
        params.append('duration=' + str(max(1, args.duration)))
    open_path = '/?' + '&'.join(params) if params else '/'

    start_http_server(port=args.port, no_browser=args.no_browser, open_path=open_path)


if __name__ == '__main__':
    main()
