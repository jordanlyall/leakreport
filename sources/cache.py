"""SQLite cache. Every fetch lands here through an idempotent upsert; the report reads only from here."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    scope TEXT NOT NULL,            -- "account:0x…" or "collection:slug"
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    ts INTEGER NOT NULL,
    tx_hash TEXT,
    raw TEXT NOT NULL,
    PRIMARY KEY (scope, event_key)
);
CREATE INDEX IF NOT EXISTS events_scope_ts ON events (scope, ts);
CREATE TABLE IF NOT EXISTS sync_state (
    scope TEXT PRIMARY KEY, last_ts INTEGER NOT NULL, complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS receipts (tx_hash TEXT PRIMARY KEY, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS txs (
    address TEXT NOT NULL, tx_hash TEXT NOT NULL, raw TEXT NOT NULL, PRIMARY KEY (address, tx_hash)
);
CREATE TABLE IF NOT EXISTS floors (
    slug TEXT NOT NULL, day TEXT NOT NULL, floor_wei TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY (slug, day, source)
);
CREATE TABLE IF NOT EXISTS eth_usd (day TEXT NOT NULL, usd REAL NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY (day, source));
CREATE TABLE IF NOT EXISTS collections (
    slug TEXT PRIMARY KEY, raw TEXT, stats TEXT, last_sale_ts INTEGER, floor_source TEXT
);
CREATE TABLE IF NOT EXISTS nfts (
    address TEXT NOT NULL, contract TEXT NOT NULL, token_id TEXT NOT NULL, raw TEXT NOT NULL,
    PRIMARY KEY (address, contract, token_id)
);
CREATE TABLE IF NOT EXISTS ens (name TEXT PRIMARY KEY, address TEXT);
CREATE TABLE IF NOT EXISTS ens_reverse (address TEXT PRIMARY KEY, name TEXT);
"""


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class Cache:
    def __init__(self, path: str | Path = "cache.sqlite"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # meta
    def set_meta(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # events
    def upsert_events(self, scope: str, rows: list[tuple[str, str, int, str | None, dict]]):
        """rows: (event_key, event_type, ts, tx_hash, raw)."""
        with self.db:
            self.db.executemany(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(scope, event_key) DO UPDATE SET "
                "event_type=excluded.event_type, ts=excluded.ts, tx_hash=excluded.tx_hash, raw=excluded.raw",
                [(scope, k, t, ts, tx, dumps(raw)) for k, t, ts, tx, raw in rows])

    def events(self, scope: str) -> list[dict]:
        rows = self.db.execute("SELECT raw FROM events WHERE scope=? ORDER BY ts, event_key", (scope,))
        return [json.loads(r[0]) for r in rows]

    def count_events(self, scope: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events WHERE scope=?", (scope,)).fetchone()[0]

    def get_sync(self, scope: str) -> tuple[int, bool] | None:
        row = self.db.execute("SELECT last_ts, complete FROM sync_state WHERE scope=?", (scope,)).fetchone()
        return (row[0], bool(row[1])) if row else None

    def set_sync(self, scope: str, last_ts: int, complete: bool):
        with self.db:
            self.db.execute("INSERT INTO sync_state VALUES (?, ?, ?) ON CONFLICT(scope) DO UPDATE SET "
                            "last_ts=excluded.last_ts, complete=excluded.complete", (scope, last_ts, int(complete)))

    # receipts and txs
    def put_receipt(self, tx_hash: str, raw: dict | None):
        """A null receipt (node without the tx) is not an answer: drop the row so a later refresh retries."""
        with self.db:
            if raw is None:
                self.db.execute("DELETE FROM receipts WHERE tx_hash=?", (tx_hash.lower(),))
                return
            self.db.execute("INSERT INTO receipts VALUES (?, ?) ON CONFLICT(tx_hash) DO UPDATE SET raw=excluded.raw",
                            (tx_hash.lower(), dumps(raw)))

    def receipt(self, tx_hash: str) -> dict | None:
        row = self.db.execute("SELECT raw FROM receipts WHERE tx_hash=?", (tx_hash.lower(),)).fetchone()
        return json.loads(row[0]) if row else None

    def has_receipt(self, tx_hash: str) -> bool:
        return self.db.execute("SELECT 1 FROM receipts WHERE tx_hash=? AND raw != 'null'",
                               (tx_hash.lower(),)).fetchone() is not None

    def put_txs(self, address: str, txs: list[dict]):
        with self.db:
            self.db.executemany(
                "INSERT INTO txs VALUES (?, ?, ?) ON CONFLICT(address, tx_hash) DO UPDATE SET raw=excluded.raw",
                [(address.lower(), t["hash"].lower(), dumps(t)) for t in txs])

    def txs(self, address: str) -> list[dict]:
        rows = self.db.execute("SELECT raw FROM txs WHERE address=? ORDER BY tx_hash", (address.lower(),))
        return [json.loads(r[0]) for r in rows]

    # prices
    def put_floors(self, slug: str, source: str, series: dict[str, int]):
        with self.db:
            self.db.executemany(
                "INSERT INTO floors VALUES (?, ?, ?, ?) ON CONFLICT(slug, day, source) DO UPDATE SET "
                "floor_wei=excluded.floor_wei", [(slug, d, str(v), source) for d, v in series.items()])

    def floors(self, slug: str, source: str) -> dict[str, int]:
        rows = self.db.execute("SELECT day, floor_wei FROM floors WHERE slug=? AND source=? ORDER BY day",
                               (slug, source))
        return {d: int(v) for d, v in rows}

    def put_eth_usd(self, source: str, series: dict[str, float]):
        with self.db:
            self.db.executemany(
                "INSERT INTO eth_usd VALUES (?, ?, ?) ON CONFLICT(day, source) DO UPDATE SET usd=excluded.usd",
                [(d, v, source) for d, v in series.items()])

    def eth_usd(self, source: str) -> dict[str, float]:
        rows = self.db.execute("SELECT day, usd FROM eth_usd WHERE source=? ORDER BY day", (source,))
        return dict(rows.fetchall())

    # collections
    def put_collection(self, slug: str, **fields):
        cols = {k: (dumps(v) if k in ("raw", "stats") and v is not None else v) for k, v in fields.items()}
        with self.db:
            self.db.execute("INSERT INTO collections (slug) VALUES (?) ON CONFLICT(slug) DO NOTHING", (slug,))
            for k, v in cols.items():
                self.db.execute(f"UPDATE collections SET {k}=? WHERE slug=?", (v, slug))

    def collection(self, slug: str) -> dict | None:
        row = self.db.execute("SELECT raw, stats, last_sale_ts, floor_source FROM collections WHERE slug=?",
                              (slug,)).fetchone()
        if not row:
            return None
        return {"raw": json.loads(row[0]) if row[0] else None, "stats": json.loads(row[1]) if row[1] else None,
                "last_sale_ts": row[2], "floor_source": row[3]}

    # holdings snapshot (replaced wholesale on refresh)
    def replace_nfts(self, address: str, nfts: list[dict]):
        with self.db:
            self.db.execute("DELETE FROM nfts WHERE address=?", (address.lower(),))
            self.db.executemany("INSERT OR REPLACE INTO nfts VALUES (?, ?, ?, ?)",
                                [(address.lower(), n["contract"].lower(), str(n["identifier"]), dumps(n))
                                 for n in nfts])

    def nfts(self, address: str) -> list[dict]:
        rows = self.db.execute("SELECT raw FROM nfts WHERE address=? ORDER BY contract, token_id",
                               (address.lower(),))
        return [json.loads(r[0]) for r in rows]

    # ENS
    def put_ens(self, name: str, address: str | None):
        with self.db:
            self.db.execute("INSERT INTO ens VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET address=excluded.address",
                            (name.lower(), address))

    def ens(self, name: str) -> str | None:
        row = self.db.execute("SELECT address FROM ens WHERE name=?", (name.lower(),)).fetchone()
        return row[0] if row else None

    def put_reverse(self, address: str, name: str | None):
        with self.db:
            self.db.execute("INSERT INTO ens_reverse VALUES (?, ?) ON CONFLICT(address) DO UPDATE SET "
                            "name=excluded.name", (address.lower(), name))

    def reverse(self, address: str) -> tuple[bool, str | None]:
        row = self.db.execute("SELECT name FROM ens_reverse WHERE address=?", (address.lower(),)).fetchone()
        return (True, row[0]) if row else (False, None)
