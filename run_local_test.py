#!/usr/bin/env python3
"""CLI helper: import M3U URL into DB and run a single check pass, printing results.

This script starts an HTTP server (serving the repo directory) so the
results HTML can fetch results.json via HTTP. The HTTP server runs in the
main thread and persists after tests finish. Tests run in a background
thread using asyncio.
"""

import sys
import json
import os
import time
import threading
import webbrowser
import asyncio
import tkinter as tk
from tkinter import ttk

from src.iptv_monitor.config import ensure_dirs
from src.iptv_monitor.worker import fetch_text, parse_m3u, check_ts
from src.iptv_monitor.db import init_db, add_channels_bulk, list_channels

RESULTS_PATH = 'results.json'
HTTP_PORT = 9001

async def run_checks_async(channels, duration_seconds=60, check_interval=1, continuous_mode=False, loop_mode='single', current_iteration=0, channels_json=None):
    """Run repeated checks for all channels concurrently for duration_seconds.

    Each channel runs in its own coroutine and updates the shared results JSON.
    If channels_json is provided, use it (preserving previous state); otherwise create new.
    """
    results = []
    if channels_json is None:
        channels_json = [
            {'name': c[1], 'status': 'pending', 'details': '', 'url': c[2], 'resolution': '', 'test_duration': '', 'tested_seconds': 0, 'issues': {'buffering': 0, 'errors': 0}, 'disconnects_count': 0, 'disconnects': [], 'buffering_total_seconds': 0.0, 'buffering_events': []} for c in channels
        ]
    test_start = int(time.time())
    # Write initial state with timestamp and duration, plus loop info
    with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
        json.dump({'channels': channels_json, 'test_start': test_start, 'test_duration': duration_seconds, 'loop_mode': loop_mode, 'current_iteration': current_iteration}, f)

    lock = asyncio.Lock()

    async def probe_channel(idx, c):
        cid, name, url = c
        start = time.time()
        buffer_count = 0
        error_count = 0
        last_notes = ''
        last_resolution = ''
        # mark running
        async with lock:
            channels_json[idx]['status'] = 'testing'
            with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
                json.dump({'channels': channels_json, 'test_start': test_start, 'test_duration': duration_seconds, 'loop_mode': loop_mode, 'current_iteration': current_iteration}, f)

        if continuous_mode:
            # initial synchronous probe to capture resolution/notes immediately
            try:
                r0, notes0, res0, t0 = await check_ts(url, per_check_timeout=min(10, duration_seconds))
                last_notes = notes0 or ''
                if res0:
                    last_resolution = res0
            except Exception:
                last_notes = ''
                last_resolution = ''
            # Run ffprobe for the entire duration and read stdout/stderr for errors/buffering
            proc = await asyncio.create_subprocess_exec(
                'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-i', url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stderr = proc.stderr
            stdout = proc.stdout
            async def read_stdout():
                nonlocal last_resolution
                last_width = None
                last_height = None
                while True:
                    line = await stdout.readline()
                    if not line:
                        break
                    text = line.decode('utf-8', errors='ignore').strip()
                    # parse width/height lines
                    if 'width=' in text:
                        import re
                        m = re.search(r'width=(\d+)', text)
                        if m:
                            last_width = m.group(1)
                    if 'height=' in text:
                        import re
                        n = re.search(r'height=(\d+)', text)
                        if n:
                            last_height = n.group(1)
                    if last_width and last_height:
                        last_resolution = f"{last_width}x{last_height}"
            async def read_stderr():
                nonlocal buffer_count, error_count, last_notes
                while True:
                    line = await stderr.readline()
                    if not line:
                        break
                    text = line.decode('utf-8', errors='ignore').strip()
                    if 'error' in text.lower() or 'failed' in text.lower():
                        error_count += 1
                        last_notes = (last_notes + ' ' + text).strip()
                    if 'buffer' in text.lower():
                        buffer_count += 1
                        last_notes = (last_notes + ' ' + text).strip()
            # start reading stdout and stderr
            reader_out = asyncio.create_task(read_stdout())
            reader_err = asyncio.create_task(read_stderr())

            # progress updater - write tested_seconds periodically while ffprobe runs
            async def update_progress():
                while True:
                    try:
                        await asyncio.sleep(5)
                        elapsed_now = time.time() - start
                        async with lock:
                            channels_json[idx]['tested_seconds'] = int(elapsed_now)
                            channels_json[idx]['resolution'] = last_resolution
                            channels_json[idx]['details'] = last_notes
                            with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
                                json.dump({'channels': channels_json, 'test_start': test_start, 'test_duration': duration_seconds, 'loop_mode': loop_mode, 'current_iteration': current_iteration}, f)
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        pass

            progress_task = asyncio.create_task(update_progress())

            try:
                await asyncio.wait_for(proc.wait(), timeout=duration_seconds)
            except asyncio.TimeoutError:
                # timeout reached; kill proc
                try:
                    proc.kill()
                except Exception:
                    pass
            
            # Cancel progress task
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            
            # Wait for readers to finish
            try:
                await asyncio.wait_for(reader_out, timeout=2)
            except Exception:
                pass
            try:
                await asyncio.wait_for(reader_err, timeout=2)
            except Exception:
                pass
            
            elapsed = time.time() - start
            # update final stats
            async with lock:
                channels_json[idx]['status'] = 'pass' if (buffer_count == 0 and error_count == 0) else 'issue'
                channels_json[idx]['details'] = last_notes
                channels_json[idx]['resolution'] = last_resolution
                channels_json[idx]['test_duration'] = ''
                channels_json[idx]['tested_seconds'] = int(elapsed)
                channels_json[idx]['issues'] = {'buffering': buffer_count, 'errors': error_count}
                channels_json[idx]['disconnects_count'] = channels_json[idx].get('disconnects_count', 0)
                channels_json[idx]['buffering_total_seconds'] = round(channels_json[idx].get('buffering_total_seconds', 0.0), 2)
                with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
                    json.dump({'channels': channels_json, 'test_start': test_start, 'test_duration': duration_seconds, 'loop_mode': loop_mode, 'current_iteration': current_iteration}, f)
            return {'id': cid, 'name': name, 'url': url, 'result': channels_json[idx]['status'], 'notes': last_notes, 'issues': channels_json[idx]['issues']}

        # non-continuous (fallback to periodic probes)
        while True:
            elapsed = time.time() - start
            if elapsed >= duration_seconds:
                break
            remaining = max(1, int(duration_seconds - elapsed))
            try:
                r, notes, resolution, single_probe_time = await check_ts(url, per_check_timeout=min(10, remaining))
            except Exception as e:
                r, notes, resolution, single_probe_time = 'error', str(e), '', None
            last_notes = notes
            if resolution:
                last_resolution = resolution
            if r == 'buffering':
                buffer_count += 1
                buff_dur = float(single_probe_time) if single_probe_time else 0.0
                async with lock:
                    channels_json[idx]['buffering_events'].append(round(buff_dur, 2))
                    channels_json[idx]['buffering_total_seconds'] += buff_dur
            if r == 'error':
                error_count += 1
                async with lock:
                    channels_json[idx]['disconnects_count'] += 1
                    channels_json[idx]['disconnects'].append(int(time.time()))
            # Update running fields under lock
            async with lock:
                channels_json[idx]['status'] = 'testing'
                channels_json[idx]['details'] = last_notes
                channels_json[idx]['resolution'] = last_resolution
                channels_json[idx]['test_duration'] = f"{single_probe_time:.2f}s" if single_probe_time else ''
                channels_json[idx]['tested_seconds'] = int(time.time() - start)
                channels_json[idx]['issues'] = {'buffering': buffer_count, 'errors': error_count}
                with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
                    json.dump({'channels': channels_json, 'test_start': test_start, 'test_duration': duration_seconds, 'loop_mode': loop_mode, 'current_iteration': current_iteration}, f)
            await asyncio.sleep(check_interval)
        # finalize
        final_status = 'pass' if (buffer_count == 0 and error_count == 0) else 'issue'
        async with lock:
            channels_json[idx]['status'] = final_status
            channels_json[idx]['details'] = last_notes
            channels_json[idx]['resolution'] = last_resolution
            channels_json[idx]['test_duration'] = channels_json[idx].get('test_duration', '')
            channels_json[idx]['tested_seconds'] = int(time.time() - start)
            channels_json[idx]['issues'] = {'buffering': buffer_count, 'errors': error_count}
            channels_json[idx]['disconnects_count'] = channels_json[idx].get('disconnects_count', 0)
            channels_json[idx]['buffering_total_seconds'] = round(channels_json[idx].get('buffering_total_seconds', 0.0), 2)
            with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
                json.dump({'channels': channels_json, 'test_start': test_start, 'test_duration': duration_seconds, 'loop_mode': loop_mode, 'current_iteration': current_iteration}, f)
        return {'id': cid, 'name': name, 'url': url, 'result': final_status, 'notes': last_notes, 'issues': channels_json[idx]['issues']}
    # Run channels sequentially to isolate IPTV server performance from local system load
    for i, ch in enumerate(channels):
        res = await probe_channel(i, ch)
        results.append(res)

    return results

async def prepare_and_run(m3u_source, duration_seconds=60, loop_mode='single', current_iteration=0):
    ensure_dirs()
    await init_db()

    # Load M3U (local file or url)

    # Read channel_selection.json if present
    selected_urls = None
    if os.path.exists('channel_selection.json'):
        try:
            with open('channel_selection.json', 'r', encoding='utf-8') as f:
                selected = json.load(f)
                selected_urls = set([c['url'] for c in selected if 'url' in c])
        except Exception as e:
            print(f"[WARN] Could not read channel_selection.json: {e}")

    if os.path.exists(m3u_source) and os.path.isfile(m3u_source):
        print('Reading local file', m3u_source)
        txt = open(m3u_source, 'r', encoding='utf-8').read()
        items = await parse_m3u(txt)
    else:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            txt = await fetch_text(session, m3u_source)
            items = await parse_m3u(txt)

    if selected_urls is not None:
        items = [item for item in items if item[1] in selected_urls]

    if not items:
        print('No channels found in M3U (or after selection)')
        return []

    ids = await add_channels_bulk(items)
    print(f'Imported {len(ids)} channels')
    channels = await list_channels()
    channels_to_check = [(c[0], c[1], c[2]) for c in channels if c[0] in set(ids)]

    if not channels_to_check:
        print('No channels to check')
        return []

    # Reset results.json and timer for new run
    test_start = int(time.time())
    
    # Load previous results if looping (to preserve buffering/disconnect counts across iterations)
    previous_counts = {}
    if current_iteration > 1 and os.path.exists(RESULTS_PATH):
        try:
            with open(RESULTS_PATH, 'r', encoding='utf-8') as f:
                prev_data = json.load(f)
                for ch in prev_data.get('channels', []):
                    previous_counts[ch['name']] = {
                        'buffering_events': ch.get('buffering_events', []),
                        'disconnects': ch.get('disconnects', []),
                        'disconnects_count': ch.get('disconnects_count', 0),
                        'buffering_total_seconds': ch.get('buffering_total_seconds', 0.0)
                    }
        except:
            pass
    
    channels_json = []
    for c in channels_to_check:
        prev = previous_counts.get(c[1], {})
        channels_json.append({
            'name': c[1],
            'status': 'pending',
            'details': '',
            'url': c[2],
            'resolution': '',
            'test_duration': '',
            'tested_seconds': 0,
            'issues': {'buffering': 0, 'errors': 0},
            'disconnects_count': prev.get('disconnects_count', 0),
            'disconnects': prev.get('disconnects', []),
            'buffering_total_seconds': prev.get('buffering_total_seconds', 0.0),
            'buffering_events': prev.get('buffering_events', [])
        })
    
    with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
        json.dump({'channels': channels_json, 'test_start': test_start, 'test_duration': duration_seconds, 'loop_mode': loop_mode, 'current_iteration': current_iteration}, f)

    print(f'Starting {len(channels_to_check)} checks (duration={duration_seconds}s)...')
    results = await run_checks_async(channels_to_check, duration_seconds=duration_seconds, loop_mode=loop_mode, current_iteration=current_iteration, channels_json=channels_json)

    print('\nResults:')
    for r in results:
        print(f"{r['name']}: {r['result']} - {r['notes']}")

    return results

# ---- Channel/Group Selection UI ----
def show_channel_selector(items):
    """Show a GUI popup to select channels/groups from M3U and test duration.
    
    Returns: tuple (list of selected channel dicts, duration_seconds)
    """
    selected_channels = []
    selected_duration = None
    
    # Parse groups from channel names
    groups = {}
    for item in items:
        name = item['name']
        # Try to extract group from name (common patterns: "Group | Channel", "[Group] Channel", etc.)
        group = 'Ungrouped'
        if ' | ' in name:
            group = name.split(' | ')[0].strip()
        elif name.startswith('[') and ']' in name:
            group = name[1:name.index(']')].strip()
        elif ' - ' in name and len(name.split(' - ')[0]) < 30:
            group = name.split(' - ')[0].strip()
        
        if group not in groups:
            groups[group] = []
        groups[group].append(item)
    
    def convert_duration():
        """Convert duration input to seconds."""
        try:
            value = float(duration_value.get())
            unit = duration_unit.get()
            
            if unit == "seconds":
                return int(value)
            elif unit == "minutes":
                return int(value * 60)
            elif unit == "hours":
                return int(value * 3600)
            elif unit == "days":
                return int(value * 86400)
            else:
                return 15  # default
        except ValueError:
            return 15  # default
    
    def on_submit():
        nonlocal selected_channels, selected_duration
        # Get all checked items
        selected_channels = []
        for group_name, channels in groups.items():
            group_var = group_vars.get(group_name)
            if group_var and group_var.get():
                # Entire group selected
                selected_channels.extend(channels)
            else:
                # Check individual channels
                for ch in channels:
                    ch_var = channel_vars.get(ch['url'])
                    if ch_var and ch_var.get():
                        selected_channels.append(ch)
        
        selected_duration = convert_duration()
        root.quit()
        root.destroy()
    
    def on_cancel():
        nonlocal selected_channels, selected_duration
        selected_channels = None
        selected_duration = None
        root.quit()
        root.destroy()
    
    def toggle_group(group_name):
        """Toggle all channels in a group."""
        state = group_vars[group_name].get()
        for ch in groups[group_name]:
            ch_var = channel_vars.get(ch['url'])
            if ch_var:
                ch_var.set(state)
    
    def on_search(*args):
        """Filter groups and channels based on search query."""
        query = search_var.get().lower()
        
        # Clear existing content
        for widget in scrollable_frame.winfo_children():
            widget.destroy()
        
        # Recreate filtered content
        for group_name in sorted(groups.keys()):
            # Check if group name matches or any channel in group matches
            group_matches = query in group_name.lower()
            matching_channels = [ch for ch in groups[group_name] if query in ch['name'].lower()]
            
            if not query or group_matches or matching_channels:
                # Show group if it matches or has matching channels
                channels_to_show = groups[group_name] if (not query or group_matches) else matching_channels
                
                group_frame = ttk.LabelFrame(scrollable_frame, text=f"{group_name} ({len(channels_to_show)} channels)", padding=10)
                group_frame.pack(fill="x", padx=10, pady=5)
                
                group_var = group_vars.get(group_name)
                if not group_var:
                    group_var = tk.BooleanVar()
                    group_vars[group_name] = group_var
                
                group_cb = ttk.Checkbutton(
                    group_frame,
                    text=f"Select all in {group_name}",
                    variable=group_var,
                    command=lambda gn=group_name: toggle_group(gn)
                )
                group_cb.pack(anchor="w")
                
                # Channel checkboxes
                channel_frame = ttk.Frame(group_frame)
                channel_frame.pack(fill="x", padx=20, pady=5)
                
                for ch in channels_to_show[:100]:  # Limit to first 100 channels per group
                    ch_var = channel_vars.get(ch['url'])
                    if not ch_var:
                        ch_var = tk.BooleanVar()
                        channel_vars[ch['url']] = ch_var
                    
                    ch_cb = ttk.Checkbutton(
                        channel_frame,
                        text=ch['name'][:100],  # Truncate long names
                        variable=ch_var
                    )
                    ch_cb.pack(anchor="w")
                
                if len(channels_to_show) > 100:
                    tk.Label(channel_frame, text=f"... and {len(channels_to_show) - 100} more channels", 
                            fg="gray").pack(anchor="w")
        
        # Update scroll region
        scrollable_frame.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))
    
    root = tk.Tk()
    root.title("Select Channels to Test")
    root.geometry("800x700")
    
    # Header
    header = tk.Label(root, text="Select channels or groups to test", font=("Arial", 14, "bold"), pady=10)
    header.pack()
    
    # Duration input frame
    duration_frame = tk.LabelFrame(root, text="Test Duration", padx=10, pady=10, font=("Arial", 10, "bold"))
    duration_frame.pack(fill="x", padx=10, pady=5)
    
    duration_input_frame = tk.Frame(duration_frame)
    duration_input_frame.pack(fill="x")
    
    tk.Label(duration_input_frame, text="Duration:", font=("Arial", 10)).pack(side="left", padx=5)
    duration_value = tk.StringVar(value="15")
    duration_entry = tk.Entry(duration_input_frame, textvariable=duration_value, font=("Arial", 10), width=10)
    duration_entry.pack(side="left", padx=5)
    
    duration_unit = tk.StringVar(value="seconds")
    unit_options = ["seconds", "minutes", "hours", "days"]
    duration_dropdown = ttk.Combobox(duration_input_frame, textvariable=duration_unit, values=unit_options, 
                                      state="readonly", width=15, font=("Arial", 10))
    duration_dropdown.pack(side="left", padx=5)
    
    # Loop mode frame
    loop_frame = tk.LabelFrame(root, text="Loop Mode", padx=10, pady=10, font=("Arial", 10, "bold"))
    loop_frame.pack(fill="x", padx=10, pady=5)
    
    loop_mode = tk.StringVar(value="single")
    loop_options = [
        ("Single Run", "single"),
        ("Loop X Times", "loop-times"),
        ("Infinite Loop", "infinite")
    ]
    
    def on_loop_mode_change(*args):
        if loop_mode.get() == "loop-times":
            loop_times_frame.pack(fill="x", pady=5)
        else:
            loop_times_frame.pack_forget()
    
    loop_mode.trace('w', on_loop_mode_change)
    
    for text, value in loop_options:
        ttk.Radiobutton(loop_frame, text=text, variable=loop_mode, value=value).pack(anchor="w")
    
    loop_times_frame = tk.Frame(loop_frame)
    tk.Label(loop_times_frame, text="Number of times:", font=("Arial", 9)).pack(side="left", padx=5)
    loop_times_value = tk.StringVar(value="2")
    loop_times_entry = tk.Entry(loop_times_frame, textvariable=loop_times_value, font=("Arial", 10), width=5)
    loop_times_entry.pack(side="left", padx=5)
    
    # Search box
    search_frame = tk.Frame(root)
    search_frame.pack(fill="x", padx=10, pady=5)
    
    tk.Label(search_frame, text="Search:", font=("Arial", 10)).pack(side="left", padx=5)
    search_var = tk.StringVar()
    search_var.trace('w', on_search)
    search_entry = tk.Entry(search_frame, textvariable=search_var, font=("Arial", 10), width=50)
    search_entry.pack(side="left", fill="x", expand=True, padx=5)
    
    # Scrollable frame
    canvas = tk.Canvas(root)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    scrollable_frame = ttk.Frame(canvas)
    
    scrollable_frame.bind(
        "<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
    )
    
    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    
    # Track checkbox variables
    group_vars = {}
    channel_vars = {}
    
    # Initial render with all groups/channels
    on_search()
    
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    
    # Buttons
    button_frame = tk.Frame(root)
    button_frame.pack(pady=10)
    
    submit_btn = tk.Button(button_frame, text="Test Selected", command=on_submit, bg="#4f8cff", fg="white", 
                          font=("Arial", 12, "bold"), padx=20, pady=10)
    submit_btn.pack(side="left", padx=5)
    
    cancel_btn = tk.Button(button_frame, text="Cancel", command=on_cancel, font=("Arial", 12), padx=20, pady=10)
    cancel_btn.pack(side="left", padx=5)
    
    root.mainloop()
    
    # Return channels, duration, and loop settings
    loop_iterations = None
    if loop_mode.get() == "loop-times":
        try:
            loop_iterations = int(loop_times_value.get())
        except ValueError:
            loop_iterations = 1
    elif loop_mode.get() == "infinite":
        loop_iterations = -1  # -1 means infinite
    
    return selected_channels, selected_duration, loop_mode.get(), loop_iterations


# ---- HTTP server helpers ----
def start_http_server(port=HTTP_PORT):
    import http.server
    import socketserver
    import subprocess

    print(f"[DEBUG] Ensuring port {port} is free before starting HTTP server...")
    # Always try to kill any process using the port with sudo before starting
    try:
        print(f"[DEBUG] Running 'sudo fuser -k {port}/tcp' to free port...")
        subprocess.run(["sudo", "fuser", "-k", f"{port}/tcp"], check=False)
    except Exception as e:
        print(f"[DEBUG] sudo fuser failed: {e}")

    # Helper to test if port is free
    import socket
    def _port_in_use(p):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", p))
            s.close()
            return False
        except OSError:
            return True

    # Retry a few times if port is still in use
    max_kill_attempts = 5
    for k in range(max_kill_attempts):
        if not _port_in_use(port):
            break
        print(f"[DEBUG] Port {port} appears in use; retrying after sudo kill (attempt {k+1}/{max_kill_attempts})...")
        time.sleep(1)
    if _port_in_use(port):
        print(f"[DEBUG] WARNING: port {port} still in use after sudo kill attempts; will proceed and retry starting server")

    Handler = http.server.SimpleHTTPRequestHandler
    for attempt in range(6):
        try:
            print(f"[DEBUG] Starting HTTP server (attempt {attempt+1})...")
            with socketserver.TCPServer(("", port), Handler) as httpd:
                print(f"[DEBUG] HTTP server started on port {port}.")
                webbrowser.open(f'http://localhost:{port}/results.html')
                httpd.serve_forever()
            break
        except OSError as e:
            print(f"[DEBUG] Port {port} is busy or failed to bind ({e}), retrying...")
            time.sleep(1)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: run_local_test.py <m3u_url_or_path>')
        sys.exit(1)

    source = sys.argv[1]
    # optional duration (seconds) argument
    if len(sys.argv) >= 3:
        try:
            duration_seconds = int(sys.argv[2])
        except Exception:
            duration_seconds = 60
    else:
        duration_seconds = 60


    # Always reset results.json and start a new test run on each launch
    def tests_runner():
        import asyncio
        # Remove old results.json if it exists to guarantee a fresh run
        try:
            if os.path.exists(RESULTS_PATH):
                os.remove(RESULTS_PATH)
        except Exception:
            pass

        # Parse M3U file to get channels
        m3u_path = source if os.path.exists(source) and os.path.isfile(source) else None
        items = []
        if m3u_path:
            with open(m3u_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            # Parse channels
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith('#EXTINF'):
                    # Extract channel name (after comma)
                    name = line.split(',', 1)[1] if ',' in line else 'Unknown Channel'
                    # next non-empty non-comment is URL
                    j = i + 1
                    while j < len(lines) and (not lines[j].strip() or lines[j].startswith('#')):
                        j += 1
                    url = lines[j].strip() if j < len(lines) else None
                    if url:
                        items.append({'name': name, 'url': url})
                    i = j
                i += 1
            
            # Show GUI selector
            print(f"\n[INFO] Found {len(items)} channels. Opening channel selector...")
            result = show_channel_selector(items)
            selected, gui_duration, loop_mode, loop_iterations = result
            
            if selected is None or len(selected) == 0:
                print("[INFO] No channels selected. Exiting.")
                return
            
            print(f"[INFO] Selected {len(selected)} channels for testing.")
            
            # Use GUI duration if provided
            if gui_duration is not None:
                duration_seconds = gui_duration
                print(f"[INFO] Test duration set to {duration_seconds} seconds")
            
            # Print loop settings
            if loop_mode == "single":
                print(f"[INFO] Loop mode: Single run")
            elif loop_mode == "loop-times":
                print(f"[INFO] Loop mode: Loop {loop_iterations} times")
            elif loop_mode == "infinite":
                print(f"[INFO] Loop mode: Infinite loop")
            
            # Write channel_selection.json for backend
            with open('channel_selection.json', 'w', encoding='utf-8') as f:
                json.dump(selected, f)
        
        # Loop implementation
        current_iteration = 0
        max_iterations = loop_iterations if loop_iterations and loop_iterations > 0 else 1
        
        while True:
            current_iteration += 1
            
            if loop_mode == "loop-times":
                print(f"\n[INFO] Starting iteration {current_iteration}/{max_iterations}")
            elif loop_mode == "infinite":
                print(f"\n[INFO] Starting iteration {current_iteration} (infinite mode)")
            
            # Run the test
            asyncio.run(prepare_and_run(source, duration_seconds=duration_seconds, loop_mode=loop_mode, current_iteration=current_iteration))
            
            # Check if we should continue looping
            if loop_mode == "single":
                break
            elif loop_mode == "loop-times" and current_iteration >= max_iterations:
                print(f"\n[INFO] Completed {max_iterations} iterations. Test finished.")
                break
            elif loop_mode == "infinite":
                print(f"\n[INFO] Iteration {current_iteration} complete. Looping again...")
                # Continue automatically

    t = threading.Thread(target=tests_runner, daemon=True)
    t.start()

    # Start HTTP server in main thread (blocking) so results.html can be fetched via HTTP
    try:
        start_http_server(port=HTTP_PORT)
    except KeyboardInterrupt:
        print('\n[INFO] HTTP server stopped.')

