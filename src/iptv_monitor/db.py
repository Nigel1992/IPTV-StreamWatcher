import aiosqlite
from .config import DB_FILE

CREATE_CHANNELS = '''
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
'''

CREATE_RESULTS = '''
CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL,
  timestamp TEXT DEFAULT (datetime('now')),
  result TEXT,
  notes TEXT,
  throughput_mbps REAL,
  startup_estimate_s REAL,
  resolution TEXT,
  framerate REAL,
  FOREIGN KEY(channel_id) REFERENCES channels(id)
);
'''

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(CREATE_CHANNELS)
        await db.execute(CREATE_RESULTS)
        # Migrate databases created by older versions.
        cur = await db.execute('PRAGMA table_info(results)')
        columns = {row[1] for row in await cur.fetchall()}
        if 'resolution' not in columns:
            await db.execute('ALTER TABLE results ADD COLUMN resolution TEXT')
        if 'framerate' not in columns:
            await db.execute('ALTER TABLE results ADD COLUMN framerate REAL')
        await db.commit()

async def add_channel(name, url):
    async with aiosqlite.connect(DB_FILE) as db:
        # avoid duplicate by url
        cur = await db.execute('SELECT id FROM channels WHERE url = ?', (url,))
        row = await cur.fetchone()
        if row:
            return row[0]
        await db.execute('INSERT INTO channels (name, url) VALUES (?,?)', (name, url))
        await db.commit()
        cur = await db.execute('SELECT last_insert_rowid()')
        r = await cur.fetchone()
        return r[0]

async def add_channels_bulk(ch_list):
    """ch_list: iterable of (name,url) - adds if not present, returns list of ids"""
    ids = []
    async with aiosqlite.connect(DB_FILE) as db:
        for name, url in ch_list:
            cur = await db.execute('SELECT id FROM channels WHERE url = ?', (url,))
            row = await cur.fetchone()
            if row:
                ids.append(row[0])
                continue
            await db.execute('INSERT INTO channels (name, url) VALUES (?,?)', (name, url))
            await db.commit()
            cur = await db.execute('SELECT last_insert_rowid()')
            r = await cur.fetchone()
            ids.append(r[0])
    return ids

async def list_channels():
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute('SELECT id, name, url FROM channels')
        rows = await cur.fetchall()
        return rows

async def insert_result(channel_id, result, notes='', throughput=None, startup=None,
                        resolution=None, framerate=None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            'INSERT INTO results '
            '(channel_id, result, notes, throughput_mbps, startup_estimate_s, resolution, framerate) '
            'VALUES (?,?,?,?,?,?,?)',
            (channel_id, result, notes, throughput, startup, resolution, framerate),
        )
        await db.commit()

async def recent_results(channel_id, window_hours=24):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute('SELECT timestamp, result, notes, throughput_mbps, startup_estimate_s FROM results WHERE channel_id=? AND timestamp >= datetime("now", ?)', (channel_id, f'-{window_hours} hours'))
        return await cur.fetchall()
