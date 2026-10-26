import asyncio
import json
import re
import shutil
import time

import aiohttp

from .db import insert_result


async def fetch_text(session, url, timeout=15):
    # Add diagnostics and retry logic
    max_attempts = 3
    delay = 2
    last_exc = None
    for attempt in range(max_attempts):
        try:
            async with session.get(url, timeout=timeout, headers={"User-Agent": "IPTVMonitor/1.0"}) as r:
                status = r.status
                ctype = r.headers.get('Content-Type', '')
                try:
                    txt = await r.text()
                except Exception as e:
                    # Try to get first bytes for diagnostics
                    raw = await r.read()
                    first_bytes = raw[:32]
                    raise Exception(f"decode error: {e}; status={status}; content-type={ctype}; first_bytes={first_bytes}")
                if status != 200:
                    raise Exception(f"HTTP {status} {ctype}")
                return txt
        except Exception as e:
            last_exc = e
            await asyncio.sleep(delay * (attempt + 1))
    raise Exception(f"fetch_text failed after {max_attempts} attempts: {last_exc}")


async def fetch_bytes(session, url, timeout=15):
    start = time.time()
    max_attempts = 2
    delay = 2
    last_exc = None
    for attempt in range(max_attempts):
        try:
            async with session.get(url, timeout=timeout, headers={"User-Agent": "IPTVMonitor/1.0"}) as r:
                status = r.status
                ctype = r.headers.get('Content-Type', '')
                if status != 200:
                    raise Exception(f"HTTP {status} {ctype}")
                data = await r.read()
                elapsed = time.time() - start
                return len(data), elapsed
        except Exception as e:
            last_exc = e
            await asyncio.sleep(delay * (attempt + 1))
    raise Exception(f"fetch_bytes failed after {max_attempts} attempts: {last_exc}")


def _guess_group(name):
    """Best-effort group extraction for playlists without group-title."""
    if ' | ' in name:
        return name.split(' | ', 1)[0].strip()
    if name.startswith('[') and ']' in name:
        return name[1:name.index(']')].strip()
    if ' - ' in name and len(name.split(' - ', 1)[0]) < 30:
        return name.split(' - ', 1)[0].strip()
    return 'Ungrouped'


def parse_m3u_items(text):
    """Parse an M3U playlist into richer channel dictionaries.

    Returns a list of dicts with name, url, group, logo and tvg_id keys.
    """
    lines = [line.strip() for line in text.splitlines()]
    items = []
    seen_urls = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXTINF'):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            name = line.split(',', 1)[1].strip() if ',' in line else ''
            name = name or attrs.get('tvg-name') or 'Unknown channel'
            group = (attrs.get('group-title') or '').strip() or _guess_group(name)
            # The next non-empty, non-comment line is the stream URL.
            j = i + 1
            while j < len(lines) and (not lines[j] or lines[j].startswith('#')):
                j += 1
            url = lines[j] if j < len(lines) else None
            if url and url not in seen_urls:
                seen_urls.add(url)
                items.append({
                    'name': name,
                    'url': url,
                    'group': group,
                    'logo': attrs.get('tvg-logo', ''),
                    'tvg_id': attrs.get('tvg-id', ''),
                })
            i = j
        i += 1
    return items


async def parse_m3u(text):
    """Parse a plain M3U playlist and return a list of (name, url) tuples."""
    return [(item['name'], item['url']) for item in parse_m3u_items(text)]


async def fetch_m3u(session, url):
    txt = await fetch_text(session, url)
    return await parse_m3u(txt)


def _parse_fraction(value):
    try:
        if value is None:
            return None
        value = str(value).strip()
        if not value or value in ('0/0', 'N/A', 'nan'):
            return None
        if '/' in value:
            numerator, denominator = value.split('/', 1)
            denominator = float(denominator)
            if denominator == 0:
                return None
            return float(numerator) / denominator
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _parse_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def resolution_label(width, height):
    """Return a short human friendly label for a video resolution."""
    if not width or not height:
        return ''
    if height >= 2160 or width >= 3840:
        return '4K'
    if height >= 1440:
        return '1440p'
    if height >= 1080:
        return '1080p'
    if height >= 720:
        return '720p'
    if height >= 576:
        return '576p'
    if height >= 480:
        return '480p'
    return f'{int(height)}p'


def format_fps(fps):
    if not fps:
        return ''
    if abs(fps - round(fps)) < 0.005:
        return f'{int(round(fps))} fps'
    return f'{fps:.2f} fps'


def _last_line(text, limit=280):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else '')[:limit]


async def analyze_smoothness(url, duration=5.0, per_check_timeout=30):
    """Decode a short slice and measure frame pacing/smoothness.

    Returns a dict with frame timing statistics and a boolean ``smooth`` flag.
    """
    started = time.time()
    result = {
        'ok': False,
        'frame_count': 0,
        'duration': 0.0,
        'avg_fps': None,
        'jitter_ms': None,
        'max_gap_ms': None,
        'duplicate_frames': 0,
        'nonmono_frames': 0,
        'interlaced_frames': 0,
        'mpdecimate_frames': None,
        'kept_frame_ratio': None,
        'duplicate_heavy': False,
        'black_frames': 0,
        'black_screen': False,
        'smooth': None,
        'notes': '',
    }

    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        result['notes'] = 'ffmpeg is not installed'
        result['duration'] = round(time.time() - started, 2)
        return result

    cmd = [
        ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'info',
        '-i', url, '-t', f'{max(1.0, float(duration)):.2f}',
        '-vf', 'showinfo', '-an', '-f', 'null', '-',
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        result['notes'] = f'Could not start ffmpeg: {e}'
        result['duration'] = round(time.time() - started, 2)
        return result

    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=max(2, per_check_timeout)
        )
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        stderr_b = b''

    stderr = (stderr_b or b'').decode('utf-8', errors='ignore')
    result['duration'] = round(time.time() - started, 2)

    if timed_out:
        result['notes'] = f'Smoothness probe timed out after {per_check_timeout}s'
        return result

    pts_times = []
    interlaced_frames = 0
    for line in stderr.splitlines():
        match = re.search(r'pts_time:([0-9]+(?:\.[0-9]+)?)', line)
        if match:
            try:
                pts_times.append(float(match.group(1)))
            except ValueError:
                pass
            if re.search(r'\bi:[TB]', line):
                interlaced_frames += 1

    result['frame_count'] = len(pts_times)
    result['interlaced_frames'] = interlaced_frames

    if len(pts_times) < 3:
        result['notes'] = 'Not enough decoded frames for pacing analysis'
        return result

    intervals = []
    duplicate_frames = 0
    nonmono_frames = 0
    for previous, current in zip(pts_times, pts_times[1:]):
        delta = current - previous
        if delta <= 0:
            if delta == 0:
                duplicate_frames += 1
            nonmono_frames += 1
            continue
        intervals.append(delta)

    if not intervals:
        result['notes'] = 'No valid frame intervals'
        return result

    avg_interval = sum(intervals) / len(intervals)
    result['avg_fps'] = round(1.0 / avg_interval, 2) if avg_interval > 0 else None
    result['duration'] = round(pts_times[-1] - pts_times[0], 2) if pts_times else result['duration']

    mean = sum(intervals) / len(intervals)
    variance = sum((value - mean) ** 2 for value in intervals) / len(intervals)
    jitter = variance ** 0.5
    result['jitter_ms'] = round(jitter * 1000, 2)
    result['max_gap_ms'] = round(max(intervals) * 1000, 2)
    result['duplicate_frames'] = duplicate_frames
    result['nonmono_frames'] = nonmono_frames
    result['ok'] = True

    # Detect duplicated frames (fake high FPS) using mpdecimate.
    try:
        mp_cmd = [
            ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-stats',
            '-i', url, '-t', f'{max(1.0, float(duration)):.2f}',
            '-vf', 'mpdecimate', '-an', '-f', 'null', '-',
        ]
        mp_proc = await asyncio.create_subprocess_exec(
            *mp_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, mp_stderr = await asyncio.wait_for(mp_proc.communicate(), timeout=max(5, per_check_timeout))
        except asyncio.TimeoutError:
            try:
                mp_proc.kill()
            except Exception:
                pass
            mp_stderr = b''
        mp_text = (mp_stderr or b'').decode('utf-8', errors='ignore')
        mp_frames = 0
        for match in re.finditer(r'frame=\s*(\d+)', mp_text):
            try:
                mp_frames = int(match.group(1))
            except ValueError:
                pass
        result['mpdecimate_frames'] = mp_frames
        if result['frame_count'] >= 10 and mp_frames > 0:
            kept_ratio = mp_frames / result['frame_count']
            result['kept_frame_ratio'] = round(kept_ratio, 3)
            if kept_ratio < 0.8:
                result['duplicate_heavy'] = True
    except Exception:
        result['mpdecimate_frames'] = None

    # Detect black screens (no picture) using the blackframe filter.
    try:
        bf_cmd = [
            ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'info',
            '-i', url, '-t', f'{max(1.0, float(duration)):.2f}',
            '-vf', 'blackframe=98:32', '-an', '-f', 'null', '-',
        ]
        bf_proc = await asyncio.create_subprocess_exec(
            *bf_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, bf_stderr = await asyncio.wait_for(bf_proc.communicate(), timeout=max(5, per_check_timeout))
        except asyncio.TimeoutError:
            try:
                bf_proc.kill()
            except Exception:
                pass
            bf_stderr = b''
        bf_text = (bf_stderr or b'').decode('utf-8', errors='ignore')
        black_frames = 0
        total_frames = 0
        for line in bf_text.splitlines():
            match = re.search(r'frame:\s*(\d+)\s+pblack:\s*(\d+)', line)
            if match:
                total_frames += 1
                try:
                    if int(match.group(2)) >= 98:
                        black_frames += 1
                except ValueError:
                    pass
        result['black_frames'] = black_frames
        if total_frames >= 5 and black_frames / total_frames >= 0.9:
            result['black_screen'] = True
    except Exception:
        pass

    expected_interval = avg_interval if avg_interval > 0 else 0.02
    max_allowed_gap = max(expected_interval * 2.0, 0.05)
    smooth = (
        result['jitter_ms'] is not None
        and result['jitter_ms'] <= 8.0
        and result['max_gap_ms'] is not None
        and result['max_gap_ms'] <= max_allowed_gap * 1000
        and duplicate_frames == 0
        and nonmono_frames == 0
        and interlaced_frames == 0
        and not result.get('duplicate_heavy')
        and not result.get('black_screen')
    )
    result['smooth'] = smooth

    notes = []
    if smooth:
        notes.append(f'Smooth pacing (jitter {result["jitter_ms"]} ms)')
    else:
        issues = []
        if result['jitter_ms'] is not None and result['jitter_ms'] > 8.0:
            issues.append(f'high jitter {result["jitter_ms"]} ms')
        if result['max_gap_ms'] is not None and result['max_gap_ms'] > max_allowed_gap * 1000:
            issues.append(f'large gap {result["max_gap_ms"]} ms')
        if duplicate_frames:
            issues.append(f'{duplicate_frames} duplicate frame(s)')
        if nonmono_frames:
            issues.append(f'{nonmono_frames} non-monotonic timestamp(s)')
        if interlaced_frames:
            issues.append(f'{interlaced_frames} interlaced frame(s)')
        if result.get('duplicate_heavy'):
            issues.append(
                f'duplicated frames (kept {result.get("mpdecimate_frames")}/{result["frame_count"]})'
            )
        if result.get('black_screen'):
            issues.append(f'black screen ({result.get("black_frames")}/{result.get("frame_count")} frames black)')
        notes.append('Not smooth: ' + ', '.join(issues) if issues else 'Pacing uncertain')
    result['notes'] = ' • '.join(notes)
    return result


async def probe_stream(url, per_check_timeout=20, check_smoothness=False, check_picture=True, stop_event=None):
    """Run ffprobe and return structured stream metadata.

    The returned dictionary always has an ``ok`` flag. Metadata keys are
    populated when ffprobe reports a video or audio stream.
    """
    started = time.time()
    info = {
        'ok': False,
        'notes': '',
        'width': None,
        'height': None,
        'resolution': '',
        'resolution_label': '',
        'fps': None,
        'framerate': '',
        'avg_fps': None,
        'real_fps': None,
        'video_codec': '',
        'audio_codec': '',
        'audio_channels': None,
        'bitrate_kbps': None,
        'duration': None,
        'field_order': '',
        'interlaced': False,
        'smoothness': None,
        'black_screen': False,
        'black_frames': 0,
        'stopped': False,
        'elapsed': 0.0,
    }

    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        info['notes'] = 'ffprobe is not installed or not in PATH'
        info['elapsed'] = round(time.time() - started, 2)
        return info

    cmd = [
        ffprobe, '-v', 'error', '-print_format', 'json',
        '-show_streams', '-show_format', '-i', url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        info['notes'] = f'Could not start ffprobe: {e}'
        info['elapsed'] = round(time.time() - started, 2)
        return info

    timed_out = False
    stopped = False
    stdout_b, stderr_b = b'', b''
    try:
        comm_task = asyncio.ensure_future(proc.communicate())
        deadline = time.time() + max(1, per_check_timeout)
        while not comm_task.done():
            if stop_event is not None and stop_event.is_set():
                stopped = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            if time.time() > deadline:
                timed_out = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            await asyncio.sleep(0.2)
        if comm_task.done():
            stdout_b, stderr_b = comm_task.result()
        else:
            try:
                stdout_b, stderr_b = await asyncio.wait_for(comm_task, timeout=3)
            except Exception:
                stdout_b, stderr_b = b'', b''
    except Exception:
        stdout_b, stderr_b = b'', b''

    stdout = stdout_b.decode('utf-8', errors='ignore') if stdout_b else ''
    stderr = stderr_b.decode('utf-8', errors='ignore') if stderr_b else ''
    info['elapsed'] = round(time.time() - started, 2)
    info['stopped'] = stopped

    if stopped:
        info['notes'] = 'Stopped'
        return info

    if timed_out:
        info['notes'] = f'Probe timed out after {per_check_timeout}s'
        return info

    if proc.returncode != 0 and not stdout:
        info['notes'] = f'ffprobe failed: {_last_line(stderr) or "no output"}'
        return info

    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        info['notes'] = f'Could not parse ffprobe output: {_last_line(stderr) or "invalid JSON"}'
        return info

    streams = data.get('streams') or []
    video = next(
        (
            stream for stream in streams
            if stream.get('codec_type') == 'video'
            and not (stream.get('disposition') or {}).get('attached_pic')
        ),
        None,
    )
    audio = next((stream for stream in streams if stream.get('codec_type') == 'audio'), None)
    fmt = data.get('format') or {}

    if video:
        width = _parse_int(video.get('width'))
        height = _parse_int(video.get('height'))
        avg_fps = _parse_fraction(video.get('avg_frame_rate'))
        real_fps = _parse_fraction(video.get('r_frame_rate'))
        fps = avg_fps or real_fps
        info.update({
            'width': width,
            'height': height,
            'resolution': f'{width}x{height}' if width and height else '',
            'resolution_label': resolution_label(width, height),
            'fps': round(fps, 3) if fps else None,
            'framerate': format_fps(fps),
            'avg_fps': round(avg_fps, 3) if avg_fps else None,
            'real_fps': round(real_fps, 3) if real_fps else None,
            'video_codec': video.get('codec_name') or '',
            'field_order': video.get('field_order') or '',
            'interlaced': bool(video.get('field_order') and video.get('field_order') not in ('progressive', 'unknown', None)),
        })

    if audio:
        info['audio_codec'] = audio.get('codec_name') or ''
        info['audio_channels'] = _parse_int(audio.get('channels'))

    bitrate = _parse_int(fmt.get('bit_rate'))
    if not bitrate and video:
        bitrate = _parse_int(video.get('bit_rate'))
    if not bitrate and video:
        bitrate = _parse_int((video.get('tags') or {}).get('variant_bitrate'))
    if bitrate:
        info['bitrate_kbps'] = round(bitrate / 1000)

    duration = fmt.get('duration')
    try:
        info['duration'] = float(duration) if duration not in (None, 'N/A') else None
    except (TypeError, ValueError):
        info['duration'] = None

    info['ok'] = bool(video or audio)

    parts = []
    if video:
        details = [info['resolution_label'] or info['resolution'] or 'video']
        if info['video_codec']:
            details.append(info['video_codec'])
        if info['framerate']:
            details.append(info['framerate'])
        avg = info.get('avg_fps')
        real = info.get('real_fps')
        if avg and real and abs(avg - real) > 0.5:
            details.append(f'VFR (avg {avg}, real {real})')
        if info.get('interlaced'):
            details.append(f'Interlaced ({info.get("field_order")})')
        parts.append('Video: ' + ' / '.join(details))
    if audio:
        audio_text = info['audio_codec'] or 'audio'
        if info['audio_channels']:
            audio_text += f' / {info["audio_channels"]}ch'
        parts.append('Audio: ' + audio_text)
    if stderr.strip():
        parts.append(_last_line(stderr))

    if check_smoothness:
        try:
            smooth = await analyze_smoothness(url, duration=5.0, per_check_timeout=per_check_timeout)
        except Exception as exc:
            smooth = {'ok': False, 'smooth': None, 'notes': f'Smoothness probe failed: {exc}'}
        info['smoothness'] = smooth
        info['black_screen'] = bool((smooth or {}).get('black_screen'))
        info['black_frames'] = (smooth or {}).get('black_frames')
        if smooth.get('notes'):
            parts.append(smooth['notes'])

    if check_picture and not check_smoothness:
        # Lightweight black-screen detection only.
        try:
            import re as _re
            _ffmpeg = shutil.which('ffmpeg')
            if not _ffmpeg:
                raise RuntimeError('ffmpeg is not installed')
            proc = await asyncio.create_subprocess_exec(
                _ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'info',
                '-i', url, '-t', '5.0', '-vf', 'blackframe=98:32',
                '-an', '-f', 'null', '-',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, bf_stderr = await asyncio.wait_for(proc.communicate(), timeout=max(5, per_check_timeout))
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                bf_stderr = b''
            bf_text = (bf_stderr or b'').decode('utf-8', errors='ignore')
            black_frames = 0
            total_frames = 0
            for line in bf_text.splitlines():
                match = _re.search(r'frame:\s*(\d+)\s+pblack:\s*(\d+)', line)
                if match:
                    total_frames += 1
                    try:
                        if int(match.group(2)) >= 98:
                            black_frames += 1
                    except ValueError:
                        pass
            info['black_frames'] = black_frames
            if total_frames >= 5 and black_frames / total_frames >= 0.9:
                info['black_screen'] = True
                parts.append(f'Black screen ({black_frames}/{total_frames} frames black)')
        except Exception as exc:
            parts.append(f'Picture check failed: {exc}')

    info['notes'] = ' • '.join(parts)
    return info


async def check_ts(url, per_check_timeout=15):
    """Backwards compatible stream check returning a 4-tuple."""
    info = await probe_stream(url, per_check_timeout=per_check_timeout)
    if info['ok']:
        status = 'pass'
    elif 'timed out' in info['notes'].lower():
        status = 'buffering'
    else:
        status = 'error'
    return status, info['notes'], info['resolution'] or None, info['elapsed']


def _looks_like_error(line):
    patterns = (
        r'\berror', r'\bfailed\b', r'\bfailure\b', r'\btimed out\b', r'\btimeout\b',
        r'\bunable\b', r'\binvalid\b', r'connection refused', r'\breconnect', r'no route',
        r'server returned [45]\d\d', r'\b(?:403|404|500|502|503)\b',
    )
    return any(re.search(pattern, line) for pattern in patterns)


async def monitor_stream(url, duration_seconds, stop_event=None):
    """Consume a stream for a while and count buffering/error events.

    ffmpeg is used (when available) so the stream is genuinely monitored for
    the requested duration instead of only being probed for metadata.
    """
    duration = max(1.0, float(duration_seconds or 1))
    started = time.time()
    result = {
        'buffering': 0,
        'buffering_events': [],
        'errors': 0,
        'drops': 0,
        'notes': '',
        'elapsed': 0.0,
        'stopped': False,
        'completed': False,
    }

    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        # Still honour the requested duration so the UI/stop button behaves.
        while time.time() - started < duration:
            if stop_event is not None and stop_event.is_set():
                result['stopped'] = True
                break
            await asyncio.sleep(0.5)
        result['elapsed'] = round(time.time() - started, 2)
        result['notes'] = 'ffmpeg not installed; stability monitoring skipped'
        return result

    cmd = [
        ffmpeg, '-hide_banner', '-nostdin', '-v', 'warning', '-stats_period', '1',
        '-i', url,
        '-t', f'{duration:.3f}',
        '-map', '0:v:0?',
        '-c:v', 'copy', '-an', '-f', 'null', '-',
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        result['errors'] = 1
        result['notes'] = f'Could not start ffmpeg: {e}'
        result['elapsed'] = round(time.time() - started, 2)
        return result

    error_lines = []

    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode('utf-8', errors='ignore').strip()
            if not text:
                continue
            lowered = text.lower()
            if 'buffer' in lowered:
                result['buffering'] += 1
                result['buffering_events'].append(round(time.time() - started, 2))
            if _looks_like_error(lowered):
                result['errors'] += 1
                if len(error_lines) < 20:
                    error_lines.append(text[:240])
            if 'drop' in lowered:
                result['drops'] += 1

    reader = asyncio.create_task(read_stderr())
    try:
        deadline = started + duration
        while True:
            if stop_event is not None and stop_event.is_set():
                result['stopped'] = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            if proc.returncode is not None:
                break
            if time.time() > deadline + 8:
                try:
                    proc.kill()
                except Exception:
                    pass
                error_lines.append('Stream monitor timed out')
                result['errors'] += 1
                break
            await asyncio.sleep(0.2)
        await proc.wait()
    finally:
        try:
            await asyncio.wait_for(reader, timeout=3)
        except Exception:
            reader.cancel()

    result['elapsed'] = round(time.time() - started, 2)

    if not result['stopped'] and proc.returncode not in (0, None):
        result['errors'] += 1
        error_lines.append(f'ffmpeg exited with code {proc.returncode}')

    result['completed'] = not result['stopped'] and proc.returncode == 0
    if error_lines:
        # Keep the most useful unique lines.
        unique = []
        for line in error_lines:
            if line not in unique:
                unique.append(line)
        result['notes'] = ' • '.join(unique)[:600]
    return result


async def check_hls(url, per_check_timeout=15):
    """Compatibility helper: probe an HLS stream and return legacy values."""
    info = await probe_stream(url, per_check_timeout=per_check_timeout)
    status = 'pass' if info['ok'] else 'error'
    return status, info['notes'], info['bitrate_kbps'], info['elapsed']


async def run_checks_concurrent(channels, concurrency=6, per_check_timeout=30):
    """Run checks concurrently with a semaphore limit. channels is iterable of (id,name,url)."""
    sem = asyncio.Semaphore(concurrency)
    results = []

    async def _run_channel(c):
        cid, name, url = c
        async with sem:
            info = await probe_stream(url, per_check_timeout=per_check_timeout)
            status = 'pass' if info['ok'] else 'error'
            await insert_result(
                cid, status, info['notes'], None, info['elapsed'],
                resolution=info['resolution'] or None, framerate=info['fps'],
            )
            return {
                'id': cid, 'name': name, 'url': url, 'result': status,
                'notes': info['notes'], 'resolution': info['resolution'],
                'framerate': info['fps'], 'startup': info['elapsed'],
            }

    tasks = [asyncio.create_task(_run_channel(c)) for c in channels]
    for task in asyncio.as_completed(tasks):
        try:
            results.append(await task)
        except Exception as e:
            results.append({
                'id': None, 'name': None, 'url': None, 'result': 'error',
                'notes': str(e), 'resolution': '', 'framerate': None, 'startup': None,
            })
    return results


class Monitor:
    def __init__(self, db, interval=900):
        self.db = db
        self.interval = interval
        self._task = None
        self._running = False

    async def _run_one(self, channel):
        cid, name, url = channel
        info = await probe_stream(url, per_check_timeout=30)
        result = 'pass' if info['ok'] else 'error'
        await insert_result(
            cid, result, info['notes'], None, info['elapsed'],
            resolution=info['resolution'] or None, framerate=info['fps'],
        )

    async def _loop(self):
        from .db import list_channels
        while self._running:
            channels = await list_channels()
            tasks = [self._run_one(c) for c in channels]
            if tasks:
                await asyncio.gather(*tasks)
            await asyncio.sleep(self.interval)

    async def run_once(self):
        """Run a single pass over all channels and return a list of results."""
        from .db import list_channels
        channels = await list_channels()
        results = []
        for c in channels:
            cid, name, url = c
            info = await probe_stream(url, per_check_timeout=30)
            result = 'pass' if info['ok'] else 'error'
            await insert_result(
                cid, result, info['notes'], None, info['elapsed'],
                resolution=info['resolution'] or None, framerate=info['fps'],
            )
            results.append({
                'id': cid, 'name': name, 'url': url, 'result': result,
                'notes': info['notes'], 'resolution': info['resolution'],
                'framerate': info['fps'], 'startup': info['elapsed'],
            })
        return results

    def start(self):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
