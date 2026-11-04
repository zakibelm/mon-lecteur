#!/usr/bin/env bash
# Déploiement idempotent du Lecteur de livres audio (Debian / Ubuntu). Peut être relancé sans danger.
#
#   1) Copier le projet COMPLET dans /opt/lecteur (app/, static/, tools/, tests/, requirements.txt, .env.example, backup.sh, deploy.sh)
#   2) sudo DOMAIN=lecteur.ton-domaine.com /opt/lecteur/deploy.sh
#      (DOMAIN est optionnel : sans lui, aucun HTTPS public n'est configuré et l'app reste sur 127.0.0.1, port 8000 ou le premier libre dès 8100)
set -euo pipefail

APP_DIR=/opt/lecteur
APP_USER=lecteur
DOMAIN="${DOMAIN:-}"
BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main

say()  { printf '\n=== %s ===\n' "$*"; }
fail() { printf '✖ %s\n' "$*" >&2; exit 1; }
warn() { printf '⚠ %s\n' "$*"; }
as_app() { runuser -u "$APP_USER" -- "$@"; }

[ "$(id -u)" -eq 0 ] || fail "Lance ce script avec sudo."
cd "$APP_DIR" 2>/dev/null || fail "$APP_DIR introuvable : copie d'abord le projet complet dedans."
for f in requirements.txt app/main.py app/settings.py app/translate.py static/index.html tools/make_icons.py tests/test_e2e.py .env.example backup.sh; do
  [ -e "$f" ] || fail "Fichier manquant : $APP_DIR/$f (copie le projet complet avant de relancer)."
done

say "1. Utilisateur dédié (sans shell de connexion)"
id "$APP_USER" &>/dev/null || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p voices data backups
chmod +x backup.sh deploy.sh
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "2. Paquets système"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng tesseract-ocr-ara \
  tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-jpn tesseract-ocr-kor ffmpeg ufw curl iproute2
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10 ou plus récent requis (version actuelle : $(python3 -V)). Ubuntu 22.04+ / Debian 12+ conviennent."

say "3. Pare-feu (le port SSH réel est détecté pour ne pas te verrouiller dehors)"
# L'app écoute sur 127.0.0.1 : aucun port à ouvrir pour elle. On n'ACTIVE jamais ufw s'il est inactif :
# sur un VPS qui héberge déjà d'autres services (n8n, proxy, Ollama…), l'activer couperait leurs ports.
# FIREWALL=enable force l'activation (serveur dédié uniquement).
SSH_PORT="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}' || true)"
SSH_PORT="${SSH_PORT:-22}"
if ufw status 2>/dev/null | grep -q '^Status: active' || [ "${FIREWALL:-auto}" = "enable" ]; then
  ufw allow "${SSH_PORT}/tcp" >/dev/null
  echo "SSH autorisé sur le port ${SSH_PORT}"
  if [ -n "$DOMAIN" ]; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi
  ufw --force enable >/dev/null
else
  warn "ufw inactif : laissé tel quel (l'activer couperait peut-être d'autres services). Aucune règle ajoutée."
fi

say "4. Environnement Python"
[ -d venv ] || as_app python3 -m venv venv
as_app venv/bin/pip install -q --upgrade pip
as_app venv/bin/pip install -q -r requirements.txt piper-tts
as_app venv/bin/python tools/make_icons.py

say "5. Voix Piper (téléchargement vérifié : un 404 n'est jamais enregistré comme voix)"
fetch() {   # fetch <destination> <url>
  [ -s "$1" ] && return 0
  as_app curl -fL --retry 3 -sS -o "$1.part" "$2" && as_app mv "$1.part" "$1"
}
get_voice() {   # get_voice <chemin-dépôt> <nom>
  fetch "voices/$2.onnx" "$BASE/$1/$2.onnx" && fetch "voices/$2.onnx.json" "$BASE/$1/$2.onnx.json" \
    && [ "$(stat -c %s "voices/$2.onnx")" -gt 1000000 ]
}
get_voice fr/fr_FR/siwis/medium fr_FR-siwis-medium || fail "Voix française indisponible (réseau ou URL)."
get_voice en/en_US/lessac/medium en_US-lessac-medium || warn "Voix anglaise non téléchargée : l'import d'un livre en anglais échouera à l'écoute."
get_voice ar/ar_JO/kareem/medium ar_JO-kareem-medium || warn "Voix arabe non téléchargée : l'import d'un livre en arabe échouera à l'écoute."
get_voice fr/fr_FR/tom/medium fr_FR-tom-medium || warn "Voix française d'homme non téléchargée : la voix de femme sera utilisée."
get_voice en/en_US/ryan/medium en_US-ryan-medium || warn "Voix anglaise d'homme non téléchargée : la voix de femme sera utilisée."

say "6. Configuration (.env)"
if [ ! -f .env ]; then
  NEW_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cp .env.example .env
  sed -i "s|^API_TOKEN=.*|API_TOKEN=${NEW_TOKEN}|" .env
  echo "🔑 Jeton généré (à saisir une fois dans l'app, note-le) : ${NEW_TOKEN}"
fi
chown "$APP_USER:$APP_USER" .env
chmod 600 .env
TOKEN="$(grep '^API_TOKEN=' .env | cut -d= -f2-)"

# Port local de l'app (127.0.0.1 seulement). Ordre : APP_PORT dans .env > port du service déjà installé >
# 8000 s'il est libre > premier port libre à partir de 8100. Un port déjà pris par un AUTRE service n'est jamais
# réutilisé (ex. 8000 servi par une autre application du VPS).
port_busy() {   # sans « ss », on tente une connexion : un port qui répond est pris
  if command -v ss >/dev/null; then ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .
  else (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; fi
}
port_owner() {   # utilisateur du processus qui écoute sur ce port (vide si libre)
  local pid; pid="$(ss -ltnpH "( sport = :$1 )" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
  [ -n "$pid" ] && ps -o user= -p "$pid" | tr -d ' '
}
APP_PORT="$(grep '^APP_PORT=' .env | cut -d= -f2- || true)"
UNIT=/etc/systemd/system/lecteur.service
if [ -z "$APP_PORT" ] && [ -f "$UNIT" ]; then    # port de l'installation précédente : gardé seulement s'il est encore à nous
  OLD="$(sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' "$UNIT" | head -1)"
  if [ -n "$OLD" ] && { ! port_busy "$OLD" || [ "$(port_owner "$OLD")" = "$APP_USER" ]; }; then APP_PORT="$OLD"
  elif [ -n "$OLD" ]; then warn "Le port ${OLD} du service précédent est pris par un autre service : nouveau port choisi."; fi
fi
if [ -z "$APP_PORT" ]; then
  for p in 8000 $(seq 8100 8199); do port_busy "$p" || { APP_PORT="$p"; break; }; done
fi
[ -n "$APP_PORT" ] || fail "Aucun port libre trouvé entre 8000 et 8199."
if port_busy "$APP_PORT" && [ "$(port_owner "$APP_PORT")" != "$APP_USER" ]; then
  fail "Le port ${APP_PORT} est déjà utilisé par un autre service : mets APP_PORT=<port libre> dans .env puis relance."
fi
if grep -q '^APP_PORT=' .env; then sed -i "s|^APP_PORT=.*|APP_PORT=${APP_PORT}|" .env; else echo "APP_PORT=${APP_PORT}" >> .env; fi
echo "Port local de l'app : 127.0.0.1:${APP_PORT}"
as_app bash -c 'set -a; . ./.env; set +a; venv/bin/python -c "from app import config; config.validate()"' \
  || fail "API_TOKEN invalide dans .env (voir message ci-dessus)."

say "7. Vérifications réelles"
OUT="$(as_app mktemp --suffix=.wav)"
if echo "Bonjour, ceci est un test." | as_app venv/bin/piper -m voices/fr_FR-siwis-medium.onnx -f "$OUT" 2>/tmp/piper_err.log \
   && [ -s "$OUT" ]; then
  echo "✔ Piper OK"
else
  cat /tmp/piper_err.log; fail "Piper en échec : corrige avant de continuer."
fi
rm -f "$OUT" /tmp/piper_err.log

if curl -fs http://127.0.0.1:11434/api/tags >/tmp/ollama_tags.json; then
  echo "✔ Ollama joignable"
  grep -q 'bge-m3' /tmp/ollama_tags.json || warn "Modèle d'embeddings absent : ollama pull bge-m3"
  grep -q 'llama3.2' /tmp/ollama_tags.json || warn "Modèle de chat absent : ollama pull llama3.2:3b"
  grep -q 'qwen2.5' /tmp/ollama_tags.json || warn "Modèle de traduction absent : ollama pull qwen2.5:7b (≈ 5 Go de RAM ; sinon choisis un modèle OpenRouter :free dans Réglages)"
else
  warn "Ollama injoignable : la lecture fonctionne, pas la discussion. Installation : curl -fsSL https://ollama.com/install.sh | sh"
fi
rm -f /tmp/ollama_tags.json

echo "Tests de bout en bout (LLM et Piper simulés)…"
as_app bash -c 'set -a; . ./.env; set +a; venv/bin/python tests/test_e2e.py' | grep -E "✔|TOUS" \
  || fail "Les tests de bout en bout échouent."

say "8. Service systemd"
cat > /etc/systemd/system/lecteur.service <<EOF
[Unit]
Description=Lecteur de livres audio
After=network.target ollama.service

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port ${APP_PORT}
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable lecteur.service >/dev/null
systemctl restart lecteur.service

ok=0
for _ in $(seq 1 20); do
  if curl -fs -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${APP_PORT}/api/config" >/dev/null; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then journalctl -u lecteur -n 30 --no-pager; fail "Le service ne répond pas."; fi
echo "✔ Service actif et authentifié"

say "9. HTTPS — seulement si DOMAIN est défini : nginx existant (site ajouté), sinon Caddy si 80/443 sont libres"
if [ -z "$DOMAIN" ]; then
  warn "DOMAIN non défini : pas d'accès public. Test possible par tunnel : ssh -L ${APP_PORT}:127.0.0.1:${APP_PORT} utilisateur@vps"
else
  BUSY="$(ss -ltnpH '( sport = :80 or sport = :443 )' 2>/dev/null | grep -v '"caddy"' || true)"
  if [ -n "$BUSY" ] && echo "$BUSY" | grep -q '"nginx"' && command -v nginx >/dev/null; then
    echo "80/443 tenus par le nginx de l'hôte : ajout d'un site dédié (aucun fichier existant modifié)."
    bash "$APP_DIR/vps/nginx-site.sh" "$DOMAIN" "$APP_PORT" || fail "Publication via nginx en échec (voir ci-dessus)."
  elif [ -n "$BUSY" ]; then
    warn "Les ports 80/443 sont déjà utilisés par un autre service (Traefik, conteneur…). Caddy n'est PAS touché."
    echo "   Ajoute à ton reverse proxy existant : ${DOMAIN} → http://127.0.0.1:${APP_PORT} (taille max du corps : MAX_UPLOAD_MB + 10 %, sans mise en tampon : proxy_request_buffering off)."
  else
    apt-get install -y -qq caddy
    BLOCK="${DOMAIN} {
    request_body {
        max_size 110MB
    }
    reverse_proxy 127.0.0.1:${APP_PORT}
}"
    if [ -f /etc/caddy/Caddyfile ] && grep -q "^${DOMAIN}" /etc/caddy/Caddyfile; then
      echo "Bloc ${DOMAIN} déjà présent dans le Caddyfile."
    else
      [ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
      printf '\n%s\n' "$BLOCK" >> /etc/caddy/Caddyfile      # ajout, jamais d'écrasement
    fi
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null || fail "Caddyfile invalide (sauvegarde : /etc/caddy/Caddyfile.bak.*)."
    systemctl enable --now caddy >/dev/null
    systemctl reload caddy
    echo "✔ HTTPS configuré. Prérequis : l'enregistrement DNS A de ${DOMAIN} doit pointer vers ce VPS."
  fi
fi

say "10. Sauvegarde quotidienne (3 h, rotation 7 jours, cache audio exclu)"
cat > /etc/cron.d/lecteur-backup <<EOF
0 3 * * * ${APP_USER} ${APP_DIR}/backup.sh >> ${APP_DIR}/backups/backup.log 2>&1
EOF
chmod 644 /etc/cron.d/lecteur-backup
as_app ./backup.sh && echo "✔ Première sauvegarde réussie"
warn "Cette sauvegarde est sur le même disque : copie-la aussi ailleurs (rclone vers un stockage distant gratuit, par exemple)."

say "TERMINÉ"
if [ -n "$DOMAIN" ]; then ADDR="https://${DOMAIN}"; else ADDR="http://127.0.0.1:${APP_PORT} (via le tunnel SSH)"; fi
echo "Ouvre ${ADDR} dans Chrome, saisis le jeton, puis menu ⋮ → Installer l'application."
