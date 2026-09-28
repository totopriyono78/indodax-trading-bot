"""`python -m bot all` — dashboard web + bot trading dalam satu container (untuk Railway, Render, Docker).

Dashboard berjalan di proses ini; bot trading dijalankan sebagai proses anak dan otomatis dinyalakan
ulang jika berhenti (termasuk setelah mode SIMULASI/LIVE diganti dari dashboard).
"""
from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time

log = logging.getLogger("bot.supervisor")


def run_all(ctx, args) -> None:
    from .web import serve

    stop = threading.Event()
    child = {"p": None}

    def bot_loop():
        backoff = 5
        while not stop.is_set():
            cmd = [sys.executable, "-m", "bot", "-c", args.config, "--env", args.env, "run", "--yes-live"]
            started = time.time()
            try:
                p = subprocess.Popen(cmd)
            except OSError as e:
                log.error("Gagal menjalankan bot: %s", e)
                stop.wait(30)
                continue
            child["p"] = p
            log.info("Bot trading berjalan (pid %s)", p.pid)
            code = p.wait()
            if stop.is_set():
                break
            if code == 75:
                log.info("Mode diganti — bot dinyalakan ulang.")
                backoff = 5
                continue
            if time.time() - started > 120:
                backoff = 5
            log.warning("Bot berhenti (kode %s). Menyalakan ulang dalam %s detik.", code, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 300)

    def shutdown(signum, frame):
        if stop.is_set():
            return
        log.info("Menerima sinyal berhenti — menghentikan bot & dashboard...")
        stop.set()
        p = child["p"]
        if p and p.poll() is None:
            p.terminate()
        raise KeyboardInterrupt  # menghentikan serve_forever()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=bot_loop, name="bot-supervisor", daemon=True).start()
    try:
        serve(ctx)
    finally:
        stop.set()
        p = child["p"]
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=25)
            except subprocess.TimeoutExpired:
                p.kill()
