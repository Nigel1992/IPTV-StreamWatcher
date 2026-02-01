# � StreamWatcher - Professional IPTV Channel Monitoring Suite

<div align="center">

![Python](https://img.shields.io/badge/python-3.13-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey.svg)
![Status](https://img.shields.io/badge/status-active-success.svg)

**Real-Time IPTV Stream Validation & Quality Monitoring Dashboard**

[Features](#-features) • [Installation](#-installation) • [Usage](#-usage) • [Configuration](#%EF%B8%8F-configuration) • [Dashboard](#-dashboard)

</div>

---

## 🎯 Overview

**StreamWatcher** is a professional-grade IPTV stream validator with a real-time web dashboard. Designed for network engineers, ISPs, and content providers who need reliable stream quality monitoring and channel validation. Features sequential testing to isolate server-side issues, cumulative quality metrics, and beautiful live analytics.

### ✨ Key Highlights

- 🔄 **Sequential Testing** - Test channels one at a time to isolate server-side performance issues
- 📊 **Real-time Dashboard** - Live updates every second with progress tracking
- 🔁 **Loop Testing** - Single run, loop X times, or infinite loop modes
- 🎬 **Resolution Detection** - Automatic 720p, 1080p, and 4K detection with icons
- 📈 **Cumulative Metrics** - Track buffering and disconnects across all loop iterations
- 🌐 **External M3U Links** - Load playlists from URLs or local files
- ⏱️ **Total Time Tracking** - Cumulative time display from initial start
- 🌓 **Dark Mode** - Toggle between light and dark themes
- 🔍 **Channel Selection UI** - Interactive GUI with group filtering and search

---

## 🚀 Features

### Testing Capabilities

- **Stream Validation** - Uses `ffprobe` to detect video/audio streams
- **Resolution Detection** - Automatically identifies 720p (HD), 1080p (Full HD), and 4K (Ultra HD)
- **Buffering Detection** - Tracks buffering events and counts occurrences
- **Disconnect Tracking** - Monitors connection failures and reconnection attempts
- **Configurable Duration** - Set custom test duration (1s - 999s) with time units (seconds/minutes/hours)
- **Sequential Execution** - Tests one channel at a time to prevent local system bottlenecks

### Loop Modes

| Mode | Description |
|------|-------------|
| **Single Run** | Test all channels once |
| **Loop X Times** | Repeat testing for a specified number of iterations (1-100+) |
| **Infinite Loop** | Continuous testing until manually stopped |

### Dashboard Features

- **Live Progress Bar** - Visual progress indicator
- **Status Indicators** - Pending (gray), Testing (blue), Pass (green), Issue (red)
- **Resolution Icons** - Visual indicators for 720p, 1080p, and 4K streams
- **Buffering Count** - Total buffering events across all loops
- **Disconnect Count** - Cumulative disconnections across iterations
- **Details Column** - Error messages, stream information, and resolution data
- **Dark Mode** - Eye-friendly dark theme with localStorage persistence
- **Responsive Design** - Adapts to different screen sizes

---

## 📦 Installation

### Prerequisites

- **Python 3.13+** (tested on 3.13.7)
- **ffprobe** (part of FFmpeg package)
- **Linux/macOS/Windows** with terminal access

### Install FFmpeg

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

**Windows:**
Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH

### Clone Repository

```bash
git clone https://github.com/Nigel1992/streamwatcher.git
cd streamwatcher
```

### Setup Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 🎮 Usage

### Basic Usage

```bash
python run_local_test.py <path_to_m3u_file_or_url>
```

**Examples:**
```bash
# Local M3U file
python run_local_test.py data/test_sample.m3u

# External M3U URL
python run_local_test.py https://example.com/playlist.m3u8
```

### Interactive Channel Selection

When you run the script, a GUI window will appear:

1. **Select Channels** - Choose channels from groups or search by name
2. **Set Duration** - Enter test duration (e.g., 15 seconds, 5 minutes, 1 hour)
3. **Choose Loop Mode**:
   - Single Run
   - Loop X Times (specify iterations)
   - Infinite Loop
4. **Click "Start Testing"**

### View Dashboard

The dashboard opens automatically in your default browser at:
```
http://localhost:9001/results.html
```

### Stop Testing

Press `Ctrl+C` in the terminal to stop the test runner.

---

## ⚙️ Configuration

### M3U File Format

Your M3U playlist should follow the standard format. The URLs can be any stream format that ffprobe supports (HLS .m3u8, direct .ts, RTSP, HTTP streams, etc.):

```m3u
#EXTM3U
#EXTINF:-1 tvg-id="channel1" tvg-name="Channel Name" tvg-logo="logo.png" group-title="Group",Channel Name
https://stream.example.com/channel1/playlist.m3u8
#EXTINF:-1 tvg-id="channel2" tvg-name="Another Channel" group-title="Group",Another Channel
http://stream.example.com/live/channel2.ts
#EXTINF:-1 tvg-id="channel3" tvg-name="RTSP Stream" group-title="Group",RTSP Stream
rtsp://stream.example.com:554/live/channel3
```

### Port Configuration

Default HTTP port: `9001`

To change the port, edit `run_local_test.py`:
```python
HTTP_PORT = 9001  # Change to your preferred port
```

### Test Duration

- **Minimum**: 1 second
- **Maximum**: No hard limit (can use days/hours/minutes/seconds units)
- **Default**: 15 seconds

### Loop Iterations

- **Loop X Times**: 1-100+ iterations
- **Infinite**: Runs until stopped manually

---

## 📊 Dashboard

### Real-time Metrics

The dashboard displays:

| Column | Description |
|--------|-------------|
| **Channel** | Channel name with resolution icon |
| **Status** | Current status (pending/testing/pass/issue) |
| **Resolution** | Detected resolution with icon (720p/1080p/4K) |
| **Tested** | Seconds tested for current iteration |
| **Disconnects** | Total disconnection count (cumulative) |
| **Buffering** | Total buffering event count (cumulative) |
| **Details** | Stream information or error messages |

### Status Colors

- 🔵 **Testing** - Currently being tested
- ⚪ **Pending** - Waiting to be tested
- 🟢 **Pass** - Stream validated successfully
- 🔴 **Issue** - Buffering or connection errors detected

### Loop Information

When using loop modes, the dashboard shows:
- **Single Run**: No loop indicator
- **Loop X Times**: "Loop Iteration: 3/5"
- **Infinite Loop**: "Infinite Loop - Iteration: 7"

---

## 🏗️ Project Structure

```
streamwatcher/
├── run_local_test.py           # Main test runner
├── results.html                # Dashboard interface
├── requirements.txt            # Python dependencies
├── data/
│   └── test_sample.m3u        # Sample M3U playlist
├── src/
│   └── iptv_monitor/
│       ├── config.py          # Configuration
│       ├── db.py              # Database operations
│       └── worker.py          # Stream testing logic
└── README.md                  # This file
```

---

## 🔧 Advanced Features

### Sequential vs Concurrent Testing

**Sequential Testing** (default):
- Tests one channel at a time
- Isolates server-side performance issues
- Prevents local system resource bottlenecks
- Recommended for accurate server validation

### Persistent Metrics Across Loops

When using loop modes, the following metrics persist across iterations:
- **Buffering Count** - Cumulative total across all loops
- **Disconnect Count** - Total disconnections across iterations
- **Total Time** - Cumulative time from initial test start

### Database Storage

Test results are stored in SQLite database:
```
iptv_monitor.db
```

Contains:
- Channel information
- Test history
- Metrics and timestamps

---

## 🐛 Troubleshooting

### Port Already in Use

If port 9001 is occupied:
```bash
sudo lsof -i :9001 | grep -v COMMAND | awk '{print $2}' | xargs -r sudo kill -9
```

Or change the port in `run_local_test.py`.

### FFprobe Not Found

Ensure FFmpeg is installed:
```bash
ffprobe -version
```

If not installed, see [Installation](#-installation) section.

### Permission Errors

On Linux, you may need to kill ports with sudo:
```bash
sudo fuser -k 9001/tcp
```

### Browser Not Opening

Manually navigate to:
```
http://localhost:9001/results.html
```

---

## 📝 Example Output

```
[INFO] Found 10 channels. Opening channel selector...
[INFO] Selected 10 channels for testing.
[INFO] Test duration set to 15 seconds
[INFO] Loop mode: Loop 5 times

[INFO] Starting iteration 1/5
Reading local file data/test_sample.m3u
Imported 10 channels
Starting 10 checks (duration=15s)...

Results:
3ABN Canada: pass - [video detected] [audio detected] [video detected 1280x720]
3ABN English: pass - [video detected] [audio detected] [video detected 1920x1080]
...

[INFO] Iteration 1 complete. Looping again...
[INFO] Starting iteration 2/5
```

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

### Development Setup

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- FFmpeg team for the excellent `ffprobe` tool
- The IPTV community for format standards and inspiration
- All contributors who help improve this project

---

## 📞 Support

If you encounter any issues or have questions:

1. Check the [Troubleshooting](#-troubleshooting) section
2. Open an [issue](https://github.com/Nigel1992/streamwatcher/issues)
3. Start a [discussion](https://github.com/Nigel1992/streamwatcher/discussions)

---

<div align="center">

**⭐ If you find this project useful, please consider giving it a star! ⭐**

Made with ❤️ by [Nigel1992](https://github.com/Nigel1992)

</div>
