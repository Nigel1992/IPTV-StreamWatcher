import asyncio
import aiohttp
import time
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

async def parse_m3u(text):
    """Parse a plain M3U playlist and return list of (name,url)"""
    lines = [l.strip() for l in text.splitlines()]
    items = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXTINF'):
            name = line.split(',', 1)[1] if ',' in line else 'unknown'
            # next non-empty non-comment is URL
            j = i + 1
            while j < len(lines) and (not lines[j] or lines[j].startswith('#')):
                j += 1
            url = lines[j] if j < len(lines) else None
            if url:
                items.append((name, url))
            i = j
        i += 1
    return items

async def fetch_m3u(session, url):
    txt = await fetch_text(session, url)
    return await parse_m3u(txt)


async def check_ts(url, per_check_timeout=15):
    """Check a direct TS stream: fetch first chunk, measure throughput, and report status."""
    import subprocess
    async def _check():
        try:
            start_time = time.time()
            ffprobe_cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-i", url]
            proc = subprocess.run(ffprobe_cmd, capture_output=True, text=True, timeout=15)
            ffprobe_out = proc.stdout
            ffprobe_err = proc.stderr
            buffering = False
            notes = ""
            resolution = ""
            duration = None
            if proc.returncode != 0:
                notes += f"ffprobe error: {ffprobe_err.strip()}"
                buffering = True
            else:
                # Extract video resolution
                import re
                if "codec_type=video" in ffprobe_out:
                    notes += "[video detected] "
                if "codec_type=audio" in ffprobe_out:
                    notes += "[audio detected] "
                w = re.search(r'width=(\d+)', ffprobe_out)
                h = re.search(r'height=(\d+)', ffprobe_out)
                if w and h:
                    resolution = f"{w.group(1)}x{h.group(1)}"
                    notes = (notes + f"[video detected {resolution}] ").strip()
                # Extract duration
                dur_match = re.search(r'duration=([\d\.]+)', ffprobe_out)
                if dur_match:
                    duration = float(dur_match.group(1))
                # Check for stalls/buffering
                if "error" in ffprobe_out or "buffer" in ffprobe_out:
                    buffering = True
                    notes += "[ffprobe buffering] "

            result = 'buffering' if buffering else 'pass'
            test_duration = time.time() - start_time
            return result, notes.strip(), resolution, test_duration
        except Exception as e:
            return 'error', str(e), None, None
    try:
        return await asyncio.wait_for(_check(), timeout=per_check_timeout)
    except asyncio.TimeoutError:
        return 'error', 'timeout', None, None
    except Exception as e:
        return 'error', str(e), None, None


async def run_checks_concurrent(channels, concurrency=6, per_check_timeout=30):
    """Run checks concurrently with a semaphore limit. channels is iterable of (id,name,url)."""
    sem = asyncio.Semaphore(concurrency)
    results = []

    async def _run_channel(c):
        cid, name, url = c
        async with sem:
            r, notes, throughput, startup = await check_hls(url, per_check_timeout=per_check_timeout)
            # store result
            await insert_result(cid, r, notes, throughput, startup)
            return {'id': cid, 'name': name, 'url': url, 'result': r, 'notes': notes, 'throughput': throughput, 'startup': startup}

    tasks = [asyncio.create_task(_run_channel(c)) for c in channels]
    for t in asyncio.as_completed(tasks):
        try:
            res = await t
            results.append(res)
        except Exception as e:
            # capture failures
            results.append({'id': None, 'name': None, 'url': None, 'result': 'error', 'notes': str(e), 'throughput': None, 'startup': None})
    return results

class Monitor:
    def __init__(self, db, interval=900):
        self.db = db
        self.interval = interval
        self._task = None
        self._running = False

    async def _run_one(self, channel):
        cid, name, url = channel
        result, notes, throughput, startup = await check_ts(url)
        await insert_result(cid, result, notes, throughput, startup)

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
            result, notes, throughput, startup = await check_ts(url)
            await insert_result(cid, result, notes, throughput, startup)
            results.append({'id': cid, 'name': name, 'url': url, 'result': result, 'notes': notes, 'throughput': throughput, 'startup': startup})
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
