import asyncio
import threading
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GObject
from .db import init_db, list_channels, add_channel, recent_results
from .worker import Monitor
from .config import DEFAULTS

class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="IPTV Monitor")
        self.set_default_size(800, 480)

        self.monitor = None
        self.loop = asyncio.get_event_loop()

        header = Gtk.HeaderBar(title="IPTV Monitor")
        header.set_show_close_button(True)
        self.set_titlebar(header)

        start_btn = Gtk.Button(label='Start Monitor')
        stop_btn = Gtk.Button(label='Stop Monitor')
        import_btn = Gtk.Button(label='Import M3U')
        start_btn.connect('clicked', self.on_start)
        stop_btn.connect('clicked', self.on_stop)
        import_btn.connect('clicked', self.on_import)
        header.pack_start(start_btn)
        header.pack_start(import_btn)
        header.pack_end(stop_btn)

        grid = Gtk.Grid()
        self.add(grid)

        # left: channels and add form
        vleft = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        grid.attach(vleft, 0, 0, 1, 1)

        self.liststore = Gtk.ListStore(int, str, str)
        tree = Gtk.TreeView(self.liststore)
        for i, title in enumerate(['id','name','url']):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, renderer, text=i)
            tree.append_column(col)
        vleft.pack_start(tree, True, True, 0)

        form = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.name_entry = Gtk.Entry(); self.name_entry.set_placeholder_text('Channel name')
        self.url_entry = Gtk.Entry(); self.url_entry.set_placeholder_text('http://.../master.m3u8')
        add_btn = Gtk.Button(label='Add')
        add_btn.connect('clicked', self.on_add)
        form.pack_start(self.name_entry, True, True, 0)
        form.pack_start(self.url_entry, True, True, 0)
        form.pack_start(add_btn, False, False, 0)
        vleft.pack_start(form, False, False, 0)

        # right: details and history
        vright = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        grid.attach(vright, 1, 0, 1, 1)

        self.status_label = Gtk.Label(label='Last run: -')
        vright.pack_start(self.status_label, False, False, 0)

        self.history_store = Gtk.ListStore(str, str, str)
        history_view = Gtk.TreeView(self.history_store)
        for i, title in enumerate(['timestamp','result','notes']):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, renderer, text=i)
            history_view.append_column(col)
        vright.pack_start(history_view, True, True, 0)

        refresh_btn = Gtk.Button(label='Refresh')
        refresh_btn.connect('clicked', lambda b: asyncio.run_coroutine_threadsafe(self.load_data(), self.loop))
        vright.pack_start(refresh_btn, False, False, 0)

    def on_import(self, _):
        dialog = Gtk.Dialog(title='Import M3U', transient_for=self, flags=0)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)
        entry = Gtk.Entry(); entry.set_placeholder_text('https://example.com/playlist.m3u or /path/to/file.m3u')
        box.add(Gtk.Label(label='Paste M3U URL or choose local file:'))
        box.add(entry)
        file_btn = Gtk.Button(label='Choose file')
        box.add(file_btn)
        def on_choose(_b):
            fc = Gtk.FileChooserDialog(title='Select M3U file', transient_for=self, action=Gtk.FileChooserAction.OPEN)
            fc.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
            resp = fc.run()
            if resp == Gtk.ResponseType.OK:
                entry.set_text(fc.get_filename())
            fc.destroy()
        file_btn.connect('clicked', on_choose)
        dialog.show_all()
        resp = dialog.run()
        source = entry.get_text().strip()
        dialog.destroy()
        if resp == Gtk.ResponseType.OK and source:
            future = asyncio.run_coroutine_threadsafe(self._import_and_run(source), self.loop)
            # show a small waiting dialog
            wait = Gtk.MessageDialog(self, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.NONE, 'Importing...')
            wait.show_all()
            try:
                results, imported = future.result(timeout=120)
            except Exception as e:
                wait.destroy()
                err = Gtk.MessageDialog(self, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, f'Import failed: {e}')
                err.run(); err.destroy()
                return
            wait.destroy()
            # refresh UI
            asyncio.run_coroutine_threadsafe(self.load_data(), self.loop)
            # show summary of imported channels and note background checks
            if results:
                msg = f'Imported {imported} channels. Ran {len(results)} checks.\n\nSample results:\n'
                for r in results[:5]:
                    msg += f"{r['name']}: {r['result']} ({r['notes']})\n"
            else:
                msg = f'Imported {imported} channels. Checks started in background — click Refresh to see results.'
            dlg = Gtk.MessageDialog(self, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.OK, msg)
            dlg.run(); dlg.destroy()

    async def _import_and_run(self, url):
        # fetch M3U and parse, add channels, and start background checks (non-blocking)
        import aiohttp
        from .worker import fetch_text, parse_m3u, run_checks_concurrent
        from .db import add_channels_bulk, list_channels
        async with aiohttp.ClientSession() as session:
            txt = await fetch_text(session, url)
            items = await parse_m3u(txt)
        if not items:
            return [], 0
        added_ids = await add_channels_bulk(items)
        # build channel tuples for checks
        all_ch = await list_channels()
        id_set = set(added_ids)
        channels_to_check = [(c[0], c[1], c[2]) for c in all_ch if c[0] in id_set]
        # start background task to run checks without blocking the UI
        # concurrency and per-check timeout are reasonable defaults
        asyncio.create_task(run_checks_concurrent(channels_to_check, concurrency=6, per_check_timeout=20))
        # return immediately so the dialog can close and UI can refresh
        return [], len(items)

        # initialize DB and load
        threading.Thread(target=self._init_async).start()

    def _init_async(self):
        asyncio.run(init_db())
        asyncio.run(self.load_data())

    async def load_data(self):
        self.liststore.clear()
        channels = await list_channels()
        for c in channels:
            self.liststore.append(list(c))

    def on_add(self, _):
        name = self.name_entry.get_text().strip()
        url = self.url_entry.get_text().strip()
        if not name or not url:
            dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, 'Name and URL required')
            dialog.run(); dialog.destroy(); return
        asyncio.run_coroutine_threadsafe(add_channel(name, url), self.loop).result()
        asyncio.run_coroutine_threadsafe(self.load_data(), self.loop)
        self.name_entry.set_text('')
        self.url_entry.set_text('')

    def on_start(self, _):
        if self.monitor and self.monitor._running:
            return
        # run monitor in asyncio background task
        self.monitor = Monitor(None, interval=DEFAULTS['check_interval_sec'])
        # start monitor as background task
        asyncio.run_coroutine_threadsafe(self._start_monitor(), self.loop)

    async def _start_monitor(self):
        self.monitor._running = True
        # start loop task
        self.monitor._task = asyncio.create_task(self.monitor._loop())

    def on_stop(self, _):
        if not self.monitor:
            return
        self.monitor.stop()

def run_app():
    win = MainWindow()
    win.connect('destroy', Gtk.main_quit)
    win.show_all()
    Gtk.main()
