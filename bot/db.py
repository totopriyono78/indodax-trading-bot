"""Database: pengaturan bot, kredensial terenkripsi, user login, sesi, dan jejak audit.

Default SQLite (file data/bot.db, tanpa server). Untuk PostgreSQL isi DATABASE_URL di .env, mis.
  DATABASE_URL=postgresql+psycopg://botuser:password@localhost:5432/indodax_bot
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as pysecrets
import time
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text, cast, create_engine, delete,
                        func, insert, select, update)
from sqlalchemy.engine import Engine as SAEngine

metadata = MetaData()

settings_t = Table(
    "settings", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),          # JSON
    Column("updated_at", Float, nullable=False),
    Column("updated_by", String(64), nullable=False, default=""),
)
secrets_t = Table(
    "secrets", metadata,
    Column("name", String(64), primary_key=True),
    Column("ciphertext", Text, nullable=False),
    Column("hint", String(32), nullable=False, default=""),
    Column("updated_at", Float, nullable=False),
    Column("updated_by", String(64), nullable=False, default=""),
)
users_t = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(64), unique=True, nullable=False),
    Column("pw_hash", String(255), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("last_login", Float),
)
sessions_t = Table(
    "sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", Integer, nullable=False),
    Column("csrf", String(64), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("ip", String(64), nullable=False, default=""),
)
audit_t = Table(
    "audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False),
    Column("username", String(64), nullable=False, default=""),
    Column("action", String(64), nullable=False),
    Column("detail", Text, nullable=False, default=""),
    Column("ip", String(64), nullable=False, default=""),
)
kv_t = Table(                         # state & status bot per mode (JSON)
    "bot_kv", metadata,
    Column("kind", String(32), primary_key=True),
    Column("mode", String(16), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", Float, nullable=False),
)
trades_t = Table(
    "trades", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False, index=True),
    Column("mode", String(16), nullable=False, index=True),
    Column("pair", String(32), nullable=False),
    Column("side", String(8), nullable=False),
    Column("qty", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("idr", Float, nullable=False),
    Column("fee_idr", Float, nullable=False, default=0),
    Column("pnl_idr", Float),
    Column("pnl_pct", Float),
    Column("reason", String(255), nullable=False, default=""),
    Column("order_id", String(128), nullable=False, default=""),
    Column("meta", Text),          # JSON: kondisi saat beli, kenaikan tertinggi/penurunan terdalam, dll.
)
logs_t = Table(
    "logs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False),
    Column("level", String(10), nullable=False),
    Column("source", String(16), nullable=False, default=""),
    Column("message", Text, nullable=False),
)
meta_t = Table(
    "meta", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", String(255), nullable=False),
)

SESSION_TTL = 12 * 3600


class SecretError(Exception):
    pass


# ---------------------------------------------------------------- password
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt_b64), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_db_url(url: str) -> str:
    """Railway/Heroku memberi `postgres://` atau `postgresql://`; SQLAlchemy butuh driver psycopg."""
    url = (url or "").strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def generate_master_key() -> str:
    return Fernet.generate_key().decode()


class Database:
    def __init__(self, url: str, master_key: Optional[str] = None):
        self.url = url
        kw = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 15}
        self.engine: SAEngine = create_engine(url, **kw)
        self._fernet = None
        self._key_error = "BOT_MASTER_KEY belum diisi di .env — jalankan `python -m bot init`."
        if master_key:
            try:
                self._fernet = Fernet(master_key.encode())
            except (ValueError, TypeError):  # kunci rusak: jangan bikin bot / dashboard gagal start
                self._key_error = "BOT_MASTER_KEY di .env tidak valid (harus kunci Fernet 44 karakter)."

    @classmethod
    def from_env(cls, data_dir: str = "data") -> "Database":
        url = normalize_db_url(os.environ.get("DATABASE_URL", ""))
        if not url:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{Path(data_dir).resolve() / 'bot.db'}"
        key = os.environ.get("BOT_MASTER_KEY", "").strip() or None
        return cls(url, key)

    def create_all(self) -> None:
        metadata.create_all(self.engine)
        self._migrate()
        if self.url.startswith("sqlite"):
            with self.engine.begin() as c:
                c.exec_driver_sql("PRAGMA journal_mode=WAL")

    def _migrate(self) -> None:
        """Tambah kolom baru pada tabel lama (create_all tidak mengubah tabel yang sudah ada)."""
        from sqlalchemy import inspect
        cols = {c["name"] for c in inspect(self.engine).get_columns("trades")}
        if "meta" not in cols:
            with self.engine.begin() as c:
                c.exec_driver_sql("ALTER TABLE trades ADD COLUMN meta TEXT")

    # ------------------------------------------------------------ settings
    def get_settings(self) -> dict:
        with self.engine.connect() as c:
            rows = c.execute(select(settings_t.c.key, settings_t.c.value)).all()
        return {k: json.loads(v) for k, v in rows}

    def put_settings(self, values: dict, user: str = "") -> None:
        now = time.time()
        with self.engine.begin() as c:
            for k, v in values.items():
                data = json.dumps(v)
                if c.execute(select(settings_t.c.key).where(settings_t.c.key == k)).first():
                    c.execute(update(settings_t).where(settings_t.c.key == k)
                              .values(value=data, updated_at=now, updated_by=user))
                else:
                    c.execute(insert(settings_t).values(key=k, value=data, updated_at=now, updated_by=user))
            self._bump(c, "config_version")

    # ------------------------------------------------------------ versi (untuk reload otomatis di bot)
    def _bump(self, c, key: str) -> None:
        # naikkan di dalam SQL (atomik) agar dua penulis bersamaan di PostgreSQL tidak menghasilkan versi sama
        r = c.execute(update(meta_t).where(meta_t.c.key == key)
                      .values(value=cast(cast(meta_t.c.value, Integer) + 1, String)))
        if not r.rowcount:
            c.execute(insert(meta_t).values(key=key, value="1"))

    def version(self, key: str) -> int:
        with self.engine.connect() as c:
            row = c.execute(select(meta_t.c.value).where(meta_t.c.key == key)).first()
        return int(row[0]) if row else 0

    def get_meta(self, key: str) -> Optional[str]:
        with self.engine.connect() as c:
            row = c.execute(select(meta_t.c.value).where(meta_t.c.key == key)).first()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.engine.begin() as c:
            if c.execute(select(meta_t.c.key).where(meta_t.c.key == key)).first():
                c.execute(update(meta_t).where(meta_t.c.key == key).values(value=value))
            else:
                c.execute(insert(meta_t).values(key=key, value=value))

    # ------------------------------------------------------------ kredensial terenkripsi
    def _need_key(self) -> Fernet:
        if not self._fernet:
            raise SecretError(self._key_error)
        return self._fernet

    def set_secret(self, name: str, value: str, user: str = "") -> None:
        f = self._need_key()
        ct = f.encrypt(value.encode()).decode()
        hint = ("••••" + value[-4:]) if len(value) >= 8 else "••••"
        now = time.time()
        with self.engine.begin() as c:
            if c.execute(select(secrets_t.c.name).where(secrets_t.c.name == name)).first():
                c.execute(update(secrets_t).where(secrets_t.c.name == name)
                          .values(ciphertext=ct, hint=hint, updated_at=now, updated_by=user))
            else:
                c.execute(insert(secrets_t).values(name=name, ciphertext=ct, hint=hint, updated_at=now,
                                                   updated_by=user))
            self._bump(c, "secrets_version")

    def delete_secret(self, name: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(secrets_t).where(secrets_t.c.name == name))
            self._bump(c, "secrets_version")

    def get_secret(self, name: str) -> Optional[str]:
        with self.engine.connect() as c:
            row = c.execute(select(secrets_t.c.ciphertext).where(secrets_t.c.name == name)).first()
        if not row:
            return None
        try:
            return self._need_key().decrypt(row[0].encode()).decode()
        except InvalidToken:
            raise SecretError("Kredensial tidak bisa didekripsi — BOT_MASTER_KEY berbeda dengan saat disimpan.")

    def secret_info(self) -> dict:
        with self.engine.connect() as c:
            rows = c.execute(select(secrets_t.c.name, secrets_t.c.hint, secrets_t.c.updated_at,
                                    secrets_t.c.updated_by)).all()
        return {r.name: {"hint": r.hint, "updated_at": r.updated_at, "updated_by": r.updated_by} for r in rows}

    # ------------------------------------------------------------ user
    def count_users(self) -> int:
        with self.engine.connect() as c:
            return c.execute(select(func.count()).select_from(users_t)).scalar_one()

    def add_user(self, username: str, password: str) -> None:
        with self.engine.begin() as c:
            c.execute(insert(users_t).values(username=username, pw_hash=hash_password(password),
                                             created_at=time.time()))

    def set_password(self, username: str, password: str) -> bool:
        with self.engine.begin() as c:
            r = c.execute(update(users_t).where(users_t.c.username == username)
                          .values(pw_hash=hash_password(password)))
            if r.rowcount:
                uid = c.execute(select(users_t.c.id).where(users_t.c.username == username)).scalar_one()
                c.execute(delete(sessions_t).where(sessions_t.c.user_id == uid))  # logout semua sesi
            return bool(r.rowcount)

    def list_users(self) -> list:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(
                select(users_t.c.username, users_t.c.created_at, users_t.c.last_login)).all()]

    def check_login(self, username: str, password: str) -> Optional[dict]:
        with self.engine.connect() as c:
            row = c.execute(select(users_t).where(users_t.c.username == username)).first()
        if not row:
            verify_password(password, hash_password("dummy"))  # samakan waktu respon
            return None
        return dict(row._mapping) if verify_password(password, row.pw_hash) else None

    # ------------------------------------------------------------ sesi
    def create_session(self, user_id: int, ip: str = "") -> tuple:
        token = pysecrets.token_urlsafe(32)
        csrf = pysecrets.token_urlsafe(24)
        now = time.time()
        with self.engine.begin() as c:
            c.execute(delete(sessions_t).where(sessions_t.c.expires_at < now))
            c.execute(insert(sessions_t).values(token_hash=_sha(token), user_id=user_id, csrf=csrf,
                                                created_at=now, expires_at=now + SESSION_TTL, ip=ip))
            c.execute(update(users_t).where(users_t.c.id == user_id).values(last_login=now))
        return token, csrf

    def get_session(self, token: str) -> Optional[dict]:
        if not token:
            return None
        with self.engine.connect() as c:
            row = c.execute(
                select(sessions_t.c.user_id, sessions_t.c.csrf, sessions_t.c.expires_at, users_t.c.username)
                .join(users_t, users_t.c.id == sessions_t.c.user_id)
                .where(sessions_t.c.token_hash == _sha(token))).first()
        if not row or row.expires_at < time.time():
            return None
        return dict(row._mapping)

    def delete_session(self, token: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(sessions_t).where(sessions_t.c.token_hash == _sha(token)))

    # ------------------------------------------------------------ audit
    def audit(self, username: str, action: str, detail: str = "", ip: str = "") -> None:
        with self.engine.begin() as c:
            c.execute(insert(audit_t).values(ts=time.time(), username=username, action=action,
                                             detail=detail[:2000], ip=ip))

    def recent_audit(self, limit: int = 50) -> list:
        with self.engine.connect() as c:
            rows = c.execute(select(audit_t).order_by(audit_t.c.id.desc()).limit(limit)).all()
        return [dict(r._mapping) for r in rows]


    # ------------------------------------------------------------ state/status bot (JSON per mode)
    def kv_get(self, kind: str, mode: str):
        with self.engine.connect() as c:
            row = c.execute(select(kv_t.c.value).where(kv_t.c.kind == kind, kv_t.c.mode == mode)).first()
        return json.loads(row[0]) if row else None

    def kv_put(self, kind: str, mode: str, value) -> None:
        data, now = json.dumps(value), time.time()
        with self.engine.begin() as c:
            r = c.execute(update(kv_t).where(kv_t.c.kind == kind, kv_t.c.mode == mode)
                          .values(value=data, updated_at=now))
            if not r.rowcount:
                c.execute(insert(kv_t).values(kind=kind, mode=mode, value=data, updated_at=now))

    # ------------------------------------------------------------ jurnal transaksi
    def add_trade(self, **row) -> None:
        with self.engine.begin() as c:
            c.execute(insert(trades_t).values(**row))

    def list_trades(self, mode: str, limit: int = 0) -> list:
        q = select(trades_t).where(trades_t.c.mode == mode)
        with self.engine.connect() as c:
            if limit:
                rows = c.execute(q.order_by(trades_t.c.id.desc()).limit(limit)).all()[::-1]
            else:
                rows = c.execute(q.order_by(trades_t.c.id)).all()
        return [dict(r._mapping) for r in rows]

    # ------------------------------------------------------------ log
    def add_logs(self, rows: list, keep: int = 3000) -> None:
        if not rows:
            return
        with self.engine.begin() as c:
            c.execute(insert(logs_t), rows)
            last = c.execute(select(func.max(logs_t.c.id))).scalar() or 0
            if last > keep:
                c.execute(delete(logs_t).where(logs_t.c.id <= last - keep))

    def tail_logs(self, n: int = 120) -> list:
        with self.engine.connect() as c:
            rows = c.execute(select(logs_t).order_by(logs_t.c.id.desc()).limit(n)).all()
        return [dict(r._mapping) for r in rows[::-1]]
