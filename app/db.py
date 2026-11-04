"""Base SQLite (livres, segments de texte, embeddings)."""
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS books(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  lang TEXT NOT NULL DEFAULT 'fr',
  status TEXT NOT NULL DEFAULT 'processing',   -- processing | ready | error
  error TEXT,
  pages_total INTEGER NOT NULL DEFAULT 0,
  pages_done INTEGER NOT NULL DEFAULT 0,
  progress_idx INTEGER NOT NULL DEFAULT 0,
  indexed INTEGER NOT NULL DEFAULT 0,          -- 1 = embeddings prêts (chat possible)
  translate INTEGER NOT NULL DEFAULT 1,        -- 1 = traduire vers le français si la langue détectée ≠ fr
  embed_sig TEXT NOT NULL DEFAULT '',          -- modèle d'embeddings utilisé pour l'index
  author TEXT NOT NULL DEFAULT '',             -- auteur lu dans les métadonnées du PDF
  title_auto INTEGER NOT NULL DEFAULT 0,       -- 1 = titre tiré du nom de fichier
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS segments(
  book_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  page INTEGER NOT NULL,
  text TEXT NOT NULL,
  lang TEXT NOT NULL DEFAULT 'und',            -- langue détectée du segment
  PRIMARY KEY(book_id, idx)
);
CREATE TABLE IF NOT EXISTS translations(
  book_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  text TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(book_id, idx)
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vocab(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  term TEXT NOT NULL,                          -- mot ou expression sélectionné
  book_id TEXT NOT NULL,
  book_title TEXT NOT NULL DEFAULT '',         -- copié : l'entrée survit à la suppression du livre
  idx INTEGER NOT NULL,
  page INTEGER NOT NULL DEFAULT 0,
  original TEXT NOT NULL DEFAULT '',           -- passage d'origine (contexte)
  translation TEXT NOT NULL DEFAULT '',        -- traduction du passage, si elle existe
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS embeddings(
  book_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  vec BLOB NOT NULL,
  PRIMARY KEY(book_id, idx)
);
"""


@contextmanager
def db():
    conn = sqlite3.connect(config.DATA_DIR / "lecteur.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "books").mkdir(exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c):
    """Migration douce depuis v2.1 : ajoute les colonnes manquantes sans toucher aux données."""
    def cols(t):
        return {r["name"] for r in c.execute(f"PRAGMA table_info({t})")}
    for table, col, ddl in (
        ("segments", "lang", "TEXT NOT NULL DEFAULT 'und'"),
        ("books", "translate", "INTEGER NOT NULL DEFAULT 1"),
        ("books", "embed_sig", "TEXT NOT NULL DEFAULT ''"),
        ("books", "author", "TEXT NOT NULL DEFAULT ''"),
        ("books", "title_auto", "INTEGER NOT NULL DEFAULT 0"),   # 1 = titre tiré du nom de fichier (remplaçable)
    ):
        if col not in cols(table):
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
