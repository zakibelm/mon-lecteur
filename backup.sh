#!/usr/bin/env bash
# Sauvegarde quotidienne : base SQLite (copie cohérente) + documents sources.
# Le cache audio est exclu (régénérable à la demande). Rotation : KEEP_DAYS jours.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/lecteur}"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-7}"
STAMP="$(date +%F)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$BACKUP_DIR"

# API de sauvegarde SQLite : copie cohérente même si le serveur écrit pendant ce temps
python3 - "$DATA_DIR/lecteur.db" "$WORK/lecteur.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY

tar -czf "$BACKUP_DIR/lecteur_$STAMP.tar.gz" --exclude='books/*/audio' \
    -C "$WORK" lecteur.db -C "$DATA_DIR" books

find "$BACKUP_DIR" -name 'lecteur_*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) sauvegarde OK : lecteur_$STAMP.tar.gz"
