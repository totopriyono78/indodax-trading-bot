"""Notifikasi ke Telegram (opsional). Jika tidak dikonfigurasi, pesan hanya masuk log."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("bot.notify")


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", enabled: bool = False, prefix: str = ""):
        self.enabled = bool(enabled and token and chat_id)
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix
        self._last_sent: dict = {}
        if enabled and not self.enabled:
            log.warning("Telegram diaktifkan di config tapi TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID kosong")

    def update(self, token: str, chat_id: str, enabled: bool) -> None:
        self.token, self.chat_id = token, chat_id
        self.enabled = bool(enabled and token and chat_id)

    def send(self, text: str, key: str = None, min_interval: float = 0) -> None:
        """Kirim pesan. `key` + `min_interval` mencegah spam untuk pesan error berulang."""
        if key and min_interval:
            if time.time() - self._last_sent.get(key, 0) < min_interval:
                return
            self._last_sent[key] = time.time()
        msg = f"{self.prefix}{text}"
        log.info("NOTIF: %s", msg.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg[:4000]},
                timeout=10,
            )
        except requests.RequestException as e:
            # pesan error requests memuat URL berisi token; bot.log tampil di dashboard
            log.warning("Gagal kirim Telegram: %s", str(e).replace(self.token, "***") if self.token else e)
