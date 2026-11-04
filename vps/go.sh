#!/usr/bin/env bash
# shellcheck disable=SC2015  # « A && res ✔ || res ✖ » est voulu : res ne peut pas échouer
# Mise en service + validations RÉELLES du Lecteur de livres audio sur le VPS, en une seule commande :
#
#   curl -fsSL https://raw.githubusercontent.com/zakibelm/mon-lecteur/ccr-1c070716-hb1ckb/vps/go.sh -o /root/lecteur-go.sh && sudo bash /root/lecteur-go.sh
#
# Options (variables d'environnement, toutes facultatives) :
#   DOMAIN=lecteur.xtream.space   sous-domaine (option A, utilisé seulement si son DNS pointe déjà vers ce VPS)
#   OMNI_KEY=...                  clé d'API OmniRoute si OmniRoute en exige une (jamais affichée)
#   OMNI_MODEL=...                route OmniRoute GRATUITE à tester/benchmarker (sinon choix prudent automatique)
#   SKIP_7B=1                     ne pas télécharger qwen2.5:7b
#
# Règles respectées : aucun service existant arrêté/redémarré/reconfiguré, aucun fichier supprimé, aucun proxy
# existant modifié ; ufw n'est pas activé s'il est inactif ; allow_paid reste FAUX ; modèle par défaut inchangé.
set -uo pipefail

REPO=https://github.com/zakibelm/mon-lecteur
BRANCH=ccr-1c070716-hb1ckb
APP=/opt/lecteur
DOMAIN="${DOMAIN:-lecteur.xtream.space}"
STAMP="$(date +%F-%H%M%S)"
LOG="/root/lecteur-rapport-$STAMP.txt"
[ "$(id -u)" -eq 0 ] || { echo "Lance avec sudo."; exit 1; }
umask 077
exec 3>&1 4>&2
exec > >(tee -a "$LOG") 2>&1

declare -a RES=()
res() { RES+=("$1 $2"); printf '%s %s\n' "$1" "$2"; }          # res ✔|✖|⚠ "message"
say() { printf '\n==================== %s ====================\n' "$*"; }
as_app() { runuser -u lecteur -- "$@"; }
val() { (cd "$APP" && as_app env OMNI_BASE_URL="${OMNI_URL:-}" OMNI_KEY="${OMNI_KEY:-}" OMNI_MODEL="${OMNI_MODEL:-}" \
          venv/bin/python tools/validate_real.py "$@"); }

# ---------------------------------------------------------------- 0. Inventaire (lecture seule)
say "0. INVENTAIRE AVANT TOUTE ACTION"
echo "--- docker ps";            docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}' 2>&1 || echo "(docker absent)"
echo "--- ss -ltnp";             ss -ltnp
echo "--- services actifs";      systemctl list-units --type=service --state=running --no-pager --no-legend | awk '{print $1}'
echo "--- ressources";           nproc; free -h; df -h / /opt 2>/dev/null
echo "--- ufw";                  ufw status 2>/dev/null | head -3 || echo "(ufw absent)"
echo "--- tailscale";            tailscale status 2>&1 | head -5 || echo "(tailscale absent)"
echo "--- OS / Python";          grep PRETTY_NAME /etc/os-release; python3 -V
PORTS_80_443="$(ss -ltnpH '( sport = :80 or sport = :443 )' 2>/dev/null)"
echo "--- 80/443 : ${PORTS_80_443:-libres}"

# ---------------------------------------------------------------- 1. Code
say "1. RÉCUPÉRATION DU CODE ($BRANCH)"
command -v git >/dev/null || apt-get install -y -qq git
SRC="/root/lecteur-src-$STAMP"
if git clone -q --depth 1 -b "$BRANCH" "$REPO" "$SRC"; then
  res ✔ "code cloné : $(git -C "$SRC" log -1 --format='%h %s')"
else
  res ✖ "clonage impossible ($REPO) — arrêt"; exit 1
fi

# ---------------------------------------------------------------- 2. Tests avant déploiement
say "2. TESTS AVANT DÉPLOIEMENT"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' && res ✔ "Python $(python3 -V | cut -d' ' -f2) ≥ 3.10" \
  || { res ✖ "Python < 3.10 — arrêt"; exit 1; }
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq shellcheck >/dev/null 2>&1
if (cd "$SRC" && LC_ALL=C.UTF-8 shellcheck deploy.sh backup.sh vps/go.sh); then res ✔ "shellcheck deploy.sh backup.sh vps/go.sh : 0 avertissement"
else res ✖ "shellcheck signale des problèmes (voir ci-dessus)"; fi
echo "(tests de bout en bout : exécutés par deploy.sh à l'étape 7, AVANT la création du service — échec = arrêt)"

# ---------------------------------------------------------------- 3. Déploiement
say "3. DÉPLOIEMENT (deploy.sh, idempotent)"
mkdir -p "$APP"
git -C "$SRC" archive HEAD | tar -x -C "$APP"          # code mis à jour ; .env, data, venv, voices conservés
PUBIP="$(curl -4 -fs --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
DNSIP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
MODE=B
if [ -n "$DNSIP" ] && [ "$DNSIP" = "$PUBIP" ]; then
  if [ -z "$PORTS_80_443" ] || echo "$PORTS_80_443" | grep -q '"caddy"'; then MODE=A; fi
fi
echo "IP publique du VPS : $PUBIP ; $DOMAIN → ${DNSIP:-aucun enregistrement} ; mode retenu : $MODE"
DEPLOY_DOMAIN=""; [ "$MODE" = A ] && DEPLOY_DOMAIN="$DOMAIN"
# le jeton éventuellement généré est masqué ici : il n'est affiché qu'une fois, à la toute fin
if DOMAIN="$DEPLOY_DOMAIN" bash "$APP/deploy.sh" 2>&1 | sed -u 's/\(Jeton généré[^:]*: \).*/\1[masqué — affiché à la fin]/'; then
  res ✔ "deploy.sh terminé (tests de bout en bout passés, service créé)"
else
  res ✖ "deploy.sh en échec — voir ci-dessus"; systemctl is-active lecteur >/dev/null || exit 1
fi
TOKEN="$(grep '^API_TOKEN=' "$APP/.env" | cut -d= -f2-)"
H=(-H "Authorization: Bearer $TOKEN")
[ "${#TOKEN}" -ge 32 ] && res ✔ "API_TOKEN de ${#TOKEN} caractères" || res ✖ "API_TOKEN trop court"
grep -nE '^[A-Z_]+=.*[[:space:]]#' "$APP/.env" && res ✖ ".env : commentaire en fin de ligne" || res ✔ ".env sans commentaire en fin de ligne"
grep -q '^PIPER_CMD=/opt/lecteur/venv/bin/piper$' "$APP/.env" && res ✔ "PIPER_CMD absolu" || res ✖ "PIPER_CMD non absolu"
pgrep -fc 'uvicorn app.main:app' | grep -qx 1 && res ✔ "un seul processus uvicorn" || res ⚠ "nombre de processus uvicorn ≠ 1"
MISSING=""; for l in chi_sim chi_tra jpn kor ara; do tesseract --list-langs 2>/dev/null | grep -qx "$l" || MISSING="$MISSING $l"; done
[ -z "$MISSING" ] && res ✔ "packs OCR chi_sim chi_tra jpn kor ara présents" || res ✖ "packs OCR manquants :$MISSING"

# ---------------------------------------------------------------- 4. Ollama
say "4. OLLAMA"
if command -v ollama >/dev/null; then OLL=(ollama)
elif OC="$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | awk 'tolower($2) ~ /ollama/ {print $1; exit}')" && [ -n "$OC" ]; then OLL=(docker exec "$OC" ollama)
else OLL=(); fi
if [ "${#OLL[@]}" -eq 0 ] || ! curl -fs --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null; then
  res ✖ "Ollama introuvable ou injoignable sur 127.0.0.1:11434 (l'app l'attend là)"
else
  for m in bge-m3 llama3.2:3b; do "${OLL[@]}" pull "$m" >/dev/null 2>&1 && res ✔ "ollama pull $m" || res ✖ "ollama pull $m"; done
  AVAIL_GB=$(awk '/MemAvailable/{printf "%d", $2/1048576}' /proc/meminfo)
  DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
  if [ "${SKIP_7B:-0}" = 1 ]; then res ⚠ "qwen2.5:7b ignoré (SKIP_7B=1)"
  elif "${OLL[@]}" list 2>/dev/null | grep -q '^qwen2.5:7b'; then res ✔ "qwen2.5:7b déjà présent (RAM disponible : ${AVAIL_GB} Go)"
  elif [ "$AVAIL_GB" -lt 6 ] || [ "$DISK_GB" -lt 8 ]; then
    res ⚠ "qwen2.5:7b NON téléchargé : RAM disponible ${AVAIL_GB} Go (≥ 6 requis) / disque ${DISK_GB} Go (≥ 8) — je ne force pas"
  else "${OLL[@]}" pull qwen2.5:7b >/dev/null 2>&1 && res ✔ "ollama pull qwen2.5:7b (RAM disponible ${AVAIL_GB} Go)" || res ✖ "ollama pull qwen2.5:7b"; fi
  "${OLL[@]}" list
fi

# ---------------------------------------------------------------- 5. Vérifications du service
say "5. SERVICE"
res "$([ "$(systemctl is-active lecteur)" = active ] && echo ✔ || echo ✖)" "systemctl is-active lecteur : $(systemctl is-active lecteur)"
CFG="$(curl -fs "${H[@]}" http://127.0.0.1:8000/api/config)" && res ✔ "GET /api/config : $CFG" || res ✖ "GET /api/config en échec"
[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/config)" = 401 ] && res ✔ "sans jeton : 401" || res ✖ "l'API répond sans jeton !"

# ---------------------------------------------------------------- 6. Accès tablette
say "6. ACCÈS DEPUIS LA TABLETTE"
URL=""
if [ "$MODE" = A ]; then
  URL="https://$DOMAIN"
  echo "Option A : $DOMAIN pointe déjà vers ce VPS et 80/443 sont libres ou à Caddy."
elif command -v tailscale >/dev/null && TSJ="$(tailscale status --json 2>/dev/null)"; then
  echo "Option B : $DOMAIN ne pointe pas vers ce VPS (${DNSIP:-NXDOMAIN}) → Tailscale Serve (HTTPS privé, sans DNS public)."
  TSNAME="$(printf '%s' "$TSJ" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
  SERVE="$(tailscale serve status 2>&1)"; echo "État Serve existant :"; echo "$SERVE"
  if echo "$SERVE" | grep -q '127.0.0.1:8000'; then
    PORT="$(echo "$SERVE" | grep -oE "$TSNAME(:[0-9]+)?" | head -1 | cut -s -d: -f2)"
  elif echo "$SERVE" | grep -qE "https://$TSNAME( |/|$)"; then
    PORT=8443                                     # 443 déjà servi par autre chose : on n'y touche pas
  else PORT=443; fi
  if ! echo "$SERVE" | grep -q '127.0.0.1:8000'; then
    if [ "$PORT" = 443 ]; then OUT="$(timeout 40 tailscale serve --bg 8000 2>&1)"; else OUT="$(timeout 40 tailscale serve --bg --https="$PORT" 8000 2>&1)"; fi
    echo "$OUT"
  fi
  URL="https://$TSNAME$([ "${PORT:-443}" != 443 ] && echo ":$PORT")"
  TSIP="$(tailscale ip -4 | head -1)"
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 --resolve "$TSNAME:${PORT:-443}:$TSIP" "$URL/manifest.webmanifest")"
  if [ "$CODE" = 200 ]; then res ✔ "HTTPS Tailscale OK ($URL/manifest.webmanifest → 200, certificat valide)"
  else res ✖ "HTTPS Tailscale non vérifié (code $CODE) — si un lien login.tailscale.com est affiché ci-dessus, ouvre-le pour activer HTTPS/Serve"; fi
else
  res ✖ "ni DNS prêt ni Tailscale : pas d'URL HTTPS"
fi

# ---------------------------------------------------------------- 7. Validations réelles 6a–6c
say "6a. PIPER (vraie synthèse fr / en / ar)"
val piper && res ✔ "Piper fr/en/ar" || res ✖ "Piper : au moins une voix en échec (détail ci-dessus)"
say "6b. PDF NATIF FRANÇAIS"
val fr && res ✔ "PDF français : pas de traduction, audio, chat RAG avec sources" || res ✖ "PDF français : au moins un point en échec"
say "6c. TRADUCTION : 3 PDF natifs + 3 PDF scannés (zh/ja/ko, détection automatique)"
val cjk && res ✔ "6 documents CJK : langue, OCR, traduction, original" || res ✖ "6c : au moins un point en échec (détail ci-dessus)"

# ---------------------------------------------------------------- 8. OmniRoute (6e)
say "6e. OMNIROUTE"
OMNI_URL=""; OMNI_CTR="$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | awk 'tolower($0) ~ /omni/ {print $1; exit}')"
if [ -z "$OMNI_CTR" ]; then
  res ✖ "aucun conteneur OmniRoute en marche (docker ps)"; ls /opt/zakomni 2>/dev/null
else
  echo "Conteneur : $OMNI_CTR"; docker port "$OMNI_CTR"
  CANDS=()
  while read -r hp; do [ -n "$hp" ] && CANDS+=("http://127.0.0.1:${hp##*:}"); done < <(docker port "$OMNI_CTR" 2>/dev/null | awk '{print $3}' | sort -u)
  for ip in $(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$OMNI_CTR"); do
    for p in $(docker inspect -f '{{range $p, $_ := .Config.ExposedPorts}}{{$p}} {{end}}' "$OMNI_CTR" | sed 's#/tcp##g; s#/udp##g'); do CANDS+=("http://$ip:$p"); done
  done
  TSIP4="$(tailscale ip -4 2>/dev/null | head -1)"
  [ -n "$TSIP4" ] && for c in "${CANDS[@]}"; do case "$c" in http://127.0.0.1:*) CANDS+=("http://$TSIP4:${c##*:}");; esac; done
  AUTHH=(); [ -n "${OMNI_KEY:-}" ] && AUTHH=(-H "Authorization: Bearer $OMNI_KEY")
  for c in "${CANDS[@]}"; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${AUTHH[@]}" "$c/v1/models")"
    echo "  essai $c/v1/models → $code"
    if [ "$code" = 200 ]; then OMNI_URL="$c/v1"; break; fi
    [ "$code" = 401 ] && NEEDKEY=1
  done
  if [ -z "$OMNI_URL" ]; then
    res ✖ "OmniRoute non joignable en HTTP 200$([ -n "${NEEDKEY:-}" ] && echo ' : clé requise (401) → relance avec OMNI_KEY=… (clé créée dans le tableau de bord OmniRoute)')"
  else
    res ✔ "OmniRoute trouvé : $OMNI_URL"
    BODY="$(python3 -c 'import json,os,sys; p={"base_url": sys.argv[1]}; k=os.environ.get("OMNI_KEY"); p.update({"api_key": k} if k else {}); print(json.dumps({"providers": {"omniroute": p}}))' "$OMNI_URL")"
    curl -fs -X PUT "${H[@]}" -H 'Content-Type: application/json' -d "$BODY" http://127.0.0.1:8000/api/settings >/dev/null \
      && res ✔ "URL OmniRoute enregistrée via PUT /api/settings" || res ✖ "PUT /api/settings (OmniRoute) refusé"
    AP="$(curl -fs "${H[@]}" http://127.0.0.1:8000/api/settings | python3 -c 'import json,sys; print(json.load(sys.stdin)["allow_paid"])')"
    [ "$AP" = False ] && res ✔ "allow_paid = False" || res ✖ "allow_paid = $AP !"
    MODELS="$(curl -fs "${H[@]}" 'http://127.0.0.1:8000/api/llm/models?provider=omniroute')"
    NM="$(printf '%s' "$MODELS" | python3 -c 'import json,sys; m=json.load(sys.stdin)["models"]; print(len(m)); [print("   ", x["id"], "gratuit" if x["free"] else ("payant" if x["free"] is False else "prix inconnu")) for x in m[:60]]')"
    echo "« Charger les modèles » : $NM"
    if [ -z "${OMNI_MODEL:-}" ]; then        # choix PRUDENT : uniquement un identifiant qui se déclare gratuit
      OMNI_MODEL="$(printf '%s' "$MODELS" | python3 -c '
import json,sys
ids=[m["id"] for m in json.load(sys.stdin)["models"]]
pick=[i for i in ids if i.endswith(":free") or "free" in i.lower()]
print(pick[0] if pick else "")')"
    fi
    if [ -z "$OMNI_MODEL" ]; then
      res ⚠ "aucune route OmniRoute ne se déclare gratuite dans son nom : test et benchmark OmniRoute NON lancés (relance avec OMNI_MODEL=<route gratuite>)"
    else
      echo "Route testée : $OMNI_MODEL"
      T0="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      ROLES_BEFORE="$(curl -fs "${H[@]}" http://127.0.0.1:8000/api/settings | python3 -c 'import json,sys; print(json.dumps({"roles": json.load(sys.stdin)["roles"]}))')"
      restore() { curl -fs -X PUT "${H[@]}" -H 'Content-Type: application/json' -d "$ROLES_BEFORE" http://127.0.0.1:8000/api/settings >/dev/null; }
      trap restore EXIT
      for role in translate chat; do         # « Tester » : bascule TEMPORAIRE du rôle, puis restauration
        PUTR="$(curl -s -X PUT "${H[@]}" -H 'Content-Type: application/json' \
          -d "{\"roles\":{\"$role\":{\"provider\":\"omniroute\",\"model\":\"$OMNI_MODEL\"}}}" http://127.0.0.1:8000/api/settings)"
        if ! echo "$PUTR" | grep -q '"omniroute"'; then res ✖ "bascule $role → OmniRoute refusée : $PUTR"; restore; continue; fi
        R="$(curl -s -X POST "${H[@]}" -H 'Content-Type: application/json' -d "{\"role\":\"$role\"}" http://127.0.0.1:8000/api/llm/test)"
        echo "  Tester $role → $R"
        echo "$R" | grep -q '"ok":true.*"model":"omniroute:' && res ✔ "OmniRoute « Tester » $role" || res ✖ "OmniRoute « Tester » $role"
        restore
      done
      EMB="$(printf '%s' "$MODELS" | python3 -c 'import json,sys; e=[m["id"] for m in json.load(sys.stdin)["models"] if "embed" in m["id"].lower() and ("free" in m["id"].lower())]; print(e[0] if e else "")')"
      if [ -n "$EMB" ]; then
        curl -fs -X PUT "${H[@]}" -H 'Content-Type: application/json' -d "{\"roles\":{\"embed\":{\"provider\":\"omniroute\",\"model\":\"$EMB\"}}}" http://127.0.0.1:8000/api/settings >/dev/null
        R="$(curl -s -X POST "${H[@]}" -H 'Content-Type: application/json' -d '{"role":"embed"}' http://127.0.0.1:8000/api/llm/test)"; echo "  Tester embed → $R"; restore
        echo "$R" | grep -q '"ok":true.*"model":"omniroute:' && res ✔ "OmniRoute « Tester » embed ($EMB)" || res ✖ "OmniRoute « Tester » embed"
      else res ⚠ "OmniRoute embed : aucune route d'embeddings gratuite identifiable → non testé"; fi
      restore; trap - EXIT
      [ "$(curl -fs "${H[@]}" http://127.0.0.1:8000/api/settings | python3 -c 'import json,sys; print(json.dumps({"roles": json.load(sys.stdin)["roles"]}))')" = "$ROLES_BEFORE" ] \
        && res ✔ "modèles par défaut restaurés à l'identique" || res ✖ "modèles par défaut NON restaurés : vérifie Réglages"
      say "6d. BENCHMARK qwen2.5:7b (Ollama) vs OmniRoute:$OMNI_MODEL — 5 passages identiques"
      val bench --omni-model "$OMNI_MODEL" && res ✔ "benchmark exécuté (temps mesurés ci-dessus)" || res ✖ "benchmark en échec"
      echo "--- Journaux OmniRoute depuis $T0 (modèle/fournisseur réellement appelés) :"
      docker logs --since "$T0" "$OMNI_CTR" 2>&1 | grep -iE 'model|provider|combo|cost|price|paid|free|route' | tail -60
      res ⚠ "garde-fou gratuit/payant aveugle aux prix OmniRoute : lis les journaux ci-dessus (aucune estimation de ma part)"
    fi
  fi
fi
if [ -z "$OMNI_URL" ] || [ -z "${OMNI_MODEL:-}" ]; then
  say "6d. BENCHMARK (Ollama seul : OmniRoute indisponible)"
  val bench && res ⚠ "benchmark qwen2.5:7b seul (OmniRoute non comparé)" || res ✖ "benchmark en échec"
fi

# ---------------------------------------------------------------- 9. Redémarrage + sauvegarde (6g)
say "6g. REDÉMARRAGE ET SAUVEGARDE"
SNAP="$(as_app mktemp --suffix=.json)"
val snapshot "$SNAP"
systemctl restart lecteur
for _ in $(seq 1 30); do curl -fs "${H[@]}" http://127.0.0.1:8000/api/config >/dev/null && break; sleep 1; done
val compare "$SNAP" && res ✔ "après systemctl restart : livres, traductions et réglages identiques" || res ✖ "données différentes après redémarrage"
if (cd "$APP" && as_app ./backup.sh); then
  ARCH="$(find "$APP/backups" -name 'lecteur_*.tar.gz' -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)"; TMPR="$(mktemp -d)"
  tar -xzf "$ARCH" -C "$TMPR"
  CHECK="$(python3 - "$TMPR/lecteur.db" "$APP/data/lecteur.db" <<'PY'
import sqlite3, sys
def counts(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("books", "segments", "translations", "settings")}, c.execute("PRAGMA integrity_check").fetchone()[0]
(r, ok), (l, _) = counts(sys.argv[1]), counts(sys.argv[2])
print(f"restauré={r} intégrité={ok} | en service={l}")
sys.exit(0 if ok == "ok" and r == l else 1)
PY
)"; rc=$?
  echo "$CHECK ; dossiers de livres restaurés : $(find "$TMPR/books" -mindepth 1 -maxdepth 1 -type d | wc -l)"
  [ $rc -eq 0 ] && res ✔ "sauvegarde $(basename "$ARCH") restaurée dans $TMPR : lisible et identique" || res ✖ "sauvegarde restaurée incohérente : $CHECK"
else res ✖ "backup.sh en échec"; fi

say "JOURNAL DU SERVICE (50 dernières lignes)"
journalctl -u lecteur -n 50 --no-pager

say "RÉSUMÉ"
printf '%s\n' "${RES[@]}"
echo
echo "Rapport complet (sans le jeton) : $LOG"

exec 1>&3 2>&4                 # le jeton ne va PAS dans le rapport
echo
echo "================ À FAIRE SUR LA TABLETTE ================"
echo "1. Ouvre dans Chrome : ${URL:-(aucune URL HTTPS : voir le résumé)}"
[ "$MODE" = B ] && echo "   (la tablette doit être connectée à Tailscale, même compte que le VPS)"
echo "2. Saisis ce jeton (affiché une seule fois) : $TOKEN"
echo "3. Menu ⋮ → « Installer l'application »"
echo "=========================================================="
