"""Log handler yang menyimpan baris log ke database (agar log tetap terlihat di dashboard setelah redeploy)."""
from __future__ import annotations

import logging
import queue
import threading
import time


class DbLogHandler(logging.Handler):
    def __init__(self, db, source: str, level=logging.INFO, flush_every: float = 3.0):
        super().__init__(level)
        self.db = db
        self.source = source
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize=5000)
        self.flush_every = flush_every
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, name="db-log", daemon=True)
        self._t.start()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("sqlalchemy", "urllib3")):
            return
        try:
            msg = self.format(record)
            self.q.put_nowait({"ts": record.created, "level": record.levelname, "source": self.source,
                               "message": msg[:4000]})
        except queue.Full:
            pass
        except Exception:
            self.handleError(record)

    def _drain(self) -> None:
        rows = []
        while True:
            try:
                rows.append(self.q.get_nowait())
            except queue.Empty:
                break
        if rows:
            try:
                self.db.add_logs(rows)
            except Exception:
                pass  # jangan sampai masalah log mengganggu bot

    def _loop(self) -> None:
        while not self._stop.wait(self.flush_every):
            self._drain()

    def close(self) -> None:
        self._stop.set()
        self._drain()
        super().close()
