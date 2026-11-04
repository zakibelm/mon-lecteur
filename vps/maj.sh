#!/usr/bin/env bash
# Met à jour le Lecteur déjà en service (code + limite d'envoi + nginx), sans rien casser :
#
#   curl -fsSL https://raw.githubusercontent.com/zakibelm/mon-lecteur/ccr-1c070716-hb1ckb/vps/maj.sh -o /root/lecteur-maj.sh && sudo bash /root/lecteur-maj.sh
#
# - code mis à jour dans /opt/lecteur (.env, data, venv, voix conservés), tests de bout en bout AVANT redémarrage ;
# - MAX_UPLOAD_MB=1024 (1 Go par fichier) dans .env ;
# - nginx : SEUL le fichier qui déclare le domaine est modifié (sauvegarde datée, nginx -t, sinon retour à l'identique).
# Variables : DOMAIN (défaut lecteur.partageons.net), MAX_MB (défaut 1024), BRANCH.
# shellcheck disable=SC2015  # « test && ok || ko » : ok ne peut pas échouer
set -euo pipefail

DOMAIN="${DOMAIN:-lecteur.partageons.net}"
MAX_MB="${MAX_MB:-1024}"
BRANCH="${BRANCH:-ccr-1c070716-hb1ckb}"
APP=/opt/lecteur
STAMP="$(date +%F-%H%M%S)"
BODY_MB=$(( MAX_MB * 11 / 10 ))
# Tests uniquement : TEST_LOCAL=1 → requêtes HTTPS vers 127.0.0.1 (certificat de test accepté)
T=(); [ "${TEST_LOCAL:-0}" = 1 ] && T=(-k --resolve "$DOMAIN:443:127.0.0.1")
ok()   { printf '✔ %s\n' "$*"; }
fail() { printf '✖ %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || fail "Lance avec sudo."
[ -f "$APP/.env" ] || fail "$APP/.env introuvable : le Lecteur n'est pas installé ici."
[[ "$MAX_MB" =~ ^[0-9]+$ ]] && [ "$MAX_MB" -ge 1 ] && [ "$MAX_MB" -le 10240 ] || fail "MAX_MB doit être un nombre de Mo entre 1 et 10240."

echo "--- 1. Code"
SRC="/root/lecteur-src-$STAMP"
git clone -q --depth 1 -b "$BRANCH" "${REPO:-https://github.com/zakibelm/mon-lecteur}" "$SRC"
ok "cloné : $(git -C "$SRC" log -1 --format='%h %s')"
tar -czf "/root/lecteur-code-avant-$STAMP.tgz" -C "$APP" --exclude=data --exclude=venv --exclude=voices --exclude=backups .
git -C "$SRC" archive HEAD | tar -x -C "$APP"
chown -R lecteur:lecteur "$APP"
ok "code copié (ancien code sauvegardé : /root/lecteur-code-avant-$STAMP.tgz)"
echo "--- 1b. Voix Piper manquantes (homme fr/en)"
VB=https://huggingface.co/rhasspy/piper-voices/resolve/main
for v in fr/fr_FR/tom/medium/fr_FR-tom-medium en/en_US/ryan/medium/en_US-ryan-medium; do
  n="${v##*/}"
  for e in onnx onnx.json; do
    f="$APP/voices/$n.$e"
    [ -s "$f" ] || { runuser -u lecteur -- curl -fL --retry 3 -sS -o "$f.part" "$VB/$v.$e" && mv "$f.part" "$f"; } \
      || printf '⚠ %s non téléchargée (la voix de femme sera utilisée)\n' "$n.$e"
  done
  [ "$(stat -c %s "$APP/voices/$n.onnx" 2>/dev/null || echo 0)" -gt 1000000 ] && ok "voix $n présente" \
    || { rm -f "$APP/voices/$n.onnx"; printf '⚠ voix %s absente\n' "$n"; }
done
chown -R lecteur:lecteur "$APP/voices"
(cd "$APP" && runuser -u lecteur -- venv/bin/python tests/test_e2e.py 2>/dev/null | grep -E "TOUS LES TESTS PASSENT") \
  || { tar -xzf "/root/lecteur-code-avant-$STAMP.tgz" -C "$APP"; fail "tests en échec : ancien code remis, service NON redémarré."; }

echo "--- 2. Limite d'envoi : ${MAX_MB} Mo par fichier"
cp -a "$APP/.env" "$APP/.env.avant-$STAMP"
if grep -q '^MAX_UPLOAD_MB=' "$APP/.env"; then sed -i "s|^MAX_UPLOAD_MB=.*|MAX_UPLOAD_MB=${MAX_MB}|" "$APP/.env"
else echo "MAX_UPLOAD_MB=${MAX_MB}" >> "$APP/.env"; fi
UNIT=/etc/systemd/system/lecteur.service    # arrêt borné à 10 s (une traduction en cours ne bloque plus le redémarrage)
if [ -f "$UNIT" ] && ! grep -q -- '--timeout-graceful-shutdown' "$UNIT"; then
  cp -a "$UNIT" "/root/lecteur.service.avant-$STAMP"
  sed -i 's|^\(ExecStart=.*uvicorn app\.main:app.*\)$|\1 --timeout-graceful-shutdown 10|' "$UNIT" && systemctl daemon-reload
  grep -q -- '--timeout-graceful-shutdown 10' "$UNIT" && ok "service : arrêt borné à 10 s (sauvegarde /root/lecteur.service.avant-$STAMP)"
fi
systemctl restart lecteur
PORT="$(grep '^APP_PORT=' "$APP/.env" | cut -d= -f2-)"; PORT="${PORT:-8000}"
TOKEN="$(grep '^API_TOKEN=' "$APP/.env" | cut -d= -f2-)"
for _ in $(seq 1 30); do curl --noproxy '*' -fs -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/config" >/dev/null && break; sleep 1; done
GOT="$(curl --noproxy '*' -fs -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/config" | grep -o '"max_upload_mb":[0-9]*' || true)"
[ "$GOT" = "\"max_upload_mb\":${MAX_MB}" ] && ok "service redémarré, l'app annonce $GOT" || fail "l'app ne répond pas comme attendu ($GOT)"

echo "--- 3. nginx (fichier du domaine uniquement)"
nginx -t >/dev/null 2>&1 || fail "configuration nginx actuelle invalide : je n'y touche pas."
FILES="$(nginx -T 2>/dev/null | awk '/^# configuration file /{cur=$4; sub(":$","",cur)} /server_name/ && $0 ~ /[[:space:]]'"${DOMAIN//./\\.}"'([[:space:];]|$)/ {print cur}' | sort -u)"
[ "$(printf '%s\n' "$FILES" | grep -c .)" = 1 ] || fail "il faut exactement UN fichier nginx qui déclare $DOMAIN (trouvés : ${FILES:-aucun})."
CONF="$(readlink -f "$FILES")"
BAK="/root/$(basename "$CONF").nginx.bak.$STAMP"
cp -a "$CONF" "$BAK"
ok "sauvegarde : $BAK"
python3 - "$CONF" "$BODY_MB" <<'PY'
import re, sys
path, body = sys.argv[1], sys.argv[2]
s = open(path).read()
# 1) taille max partout où elle est déclarée dans CE fichier
s, n = re.subn(r"client_max_body_size\s+[^;]+;", f"client_max_body_size {body}m;", s)
# 2) dans chaque bloc « location » qui fait proxy_pass : transmission au fil de l'eau (sans copie disque)
def add(m):
    blk = m.group(0)
    if "proxy_pass" not in blk or "proxy_request_buffering" in blk:
        return blk
    return re.sub(r"(\n(\s*)proxy_pass[^;]*;)", r"\1\n\2proxy_request_buffering off;", blk, count=1)
s = re.sub(r"location\s+[^{]*\{[^{}]*\}", add, s)
# 3) serveur HTTPS : délai d'attente entre deux morceaux reçus (4G) et taille si jamais absente
def srv(m):
    blk = m.group(0)
    if "listen" in blk and "443" in blk:
        extra = ""
        if "client_body_timeout" not in blk: extra += "\n    client_body_timeout 300s;"
        if "client_max_body_size" not in blk: extra += f"\n    client_max_body_size {body}m;"
        blk = blk.replace("{", "{" + extra, 1)
    return blk
s = re.sub(r"server\s*\{(?:[^{}]|\{[^{}]*\})*\}", srv, s)
open(path, "w").write(s)
PY
if ! nginx -t 2>/tmp/nginx-maj.log; then
  cat /tmp/nginx-maj.log; cp -a "$BAK" "$CONF"
  cp -a "$APP/.env.avant-$STAMP" "$APP/.env"; systemctl restart lecteur     # app et nginx restent cohérents
  fail "nginx -t refuse la modification : fichier nginx et .env remis à l'identique, nginx NON rechargé."
fi
systemctl reload nginx
ok "nginx -t valide, rechargé ; dans $CONF :"
grep -nE "client_max_body_size|proxy_request_buffering|client_body_timeout" "$CONF"

echo "--- 4. Vérifications"
sleep 2
c="$(curl --noproxy '*' -s "${T[@]}" -o /dev/null -w '%{http_code}' "https://$DOMAIN/manifest.webmanifest")"
[ "$c" = 200 ] && ok "https://$DOMAIN/manifest.webmanifest → 200" || fail "https://$DOMAIN → $c"
c="$(curl --noproxy '*' -s "${T[@]}" -o /dev/null -w '%{http_code}' "https://$DOMAIN/api/config")"
[ "$c" = 401 ] && ok "API sans jeton → 401" || fail "API sans jeton → $c"
F="$(mktemp --suffix=.pdf)"; head -c $(( 400 * 1048576 )) /dev/urandom > "$F"
c="$(curl --noproxy '*' -s "${T[@]}" -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
     -F "file=@$F;filename=essai-400Mo.txt" "https://$DOMAIN/api/books")"
rm -f "$F"
# .txt est refusé par l'app (400) APRÈS réception complète : prouve que nginx laisse passer 400 Mo (sinon 413)
[ "$c" = 400 ] && ok "envoi de 400 Mo : traversé nginx jusqu'à l'app (400 = format refusé exprès, pas 413)" \
  || fail "envoi de 400 Mo → $c (413 = encore bloqué par nginx)"
echo "Retour arrière : cp -a $BAK $CONF && nginx -t && systemctl reload nginx ; cp $APP/.env.avant-$STAMP $APP/.env"
ok "Mise à jour terminée : https://$DOMAIN (jusqu'à ${MAX_MB} Mo par fichier, plusieurs fichiers à la fois)"
