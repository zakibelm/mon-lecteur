#!/usr/bin/env bash
# shellcheck disable=SC2015  # « test && ok || fail » : ok ne peut pas échouer
# Publie le Lecteur sur https://DOMAINE via le nginx DÉJÀ installé sur le VPS (option A).
#
#   sudo bash vps/nginx-site.sh lecteur.partageons.net [PORT_APP]
#
# Ne casse rien : sauvegarde datée de /etc/nginx ; AJOUT d'un fichier de site neuf (aucun fichier existant modifié) ;
# `nginx -t` avant chaque rechargement (en cas d'échec : notre fichier est mis de côté en .desactive, nginx n'est pas
# rechargé) ; codes HTTP des autres sites relevés avant/après pour prouver qu'ils répondent pareil.
# Certificat : Let's Encrypt en mode webroot (certbot ne réécrit aucune config nginx), renouvelé automatiquement.
# Variables : CERTBOT_EMAIL (facultatif), SKIP_CERTBOT=1 (tests : certificat déjà présent dans CERT_DIR).
set -euo pipefail

DOMAIN="${1:?usage : nginx-site.sh DOMAINE [PORT_APP]}"
APP_PORT="${2:-$(grep '^APP_PORT=' /opt/lecteur/.env 2>/dev/null | cut -d= -f2-)}"
APP_PORT="${APP_PORT:-8000}"
MAX_MB="$(grep '^MAX_UPLOAD_MB=' /opt/lecteur/.env 2>/dev/null | cut -d= -f2-)"
BODY_MB=$(( ${MAX_MB:-1024} * 11 / 10 ))
STAMP="$(date +%F-%H%M%S)"
WEBROOT=/var/www/lecteur-acme
CERT_DIR="${CERT_DIR:-/etc/letsencrypt/live/$DOMAIN}"
say()  { printf '\n--- %s\n' "$*"; }
fail() { printf '✖ %s\n' "$*" >&2; exit 1; }
ok()   { printf '✔ %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || fail "Lance avec sudo."
command -v nginx >/dev/null || fail "nginx n'est pas installé sur l'hôte (s'il tourne dans Docker, ajoute la route à la main)."
if command -v ss >/dev/null && ss -ltnpH '( sport = :443 )' | grep -q 'docker-proxy'; then
  fail "Le port 443 est tenu par un conteneur Docker, pas par le nginx de l'hôte : ajout automatique impossible."
fi
curl --noproxy '*' -fs -o /dev/null --max-time 5 "http://127.0.0.1:${APP_PORT}/manifest.webmanifest" \
  || fail "Le Lecteur ne répond pas sur 127.0.0.1:${APP_PORT}."

reload_nginx() { systemctl reload nginx 2>/dev/null || nginx -s reload; }

# Emplacement d'un site neuf, selon l'organisation déjà en place
if [ -d /etc/nginx/sites-enabled ] && nginx -T 2>/dev/null | grep -qE '^\s*include\s+/etc/nginx/sites-enabled/'; then
  AVAIL="/etc/nginx/sites-available/lecteur-${DOMAIN}.conf"; ENABLED="/etc/nginx/sites-enabled/lecteur-${DOMAIN}.conf"
else
  AVAIL="/etc/nginx/conf.d/lecteur-${DOMAIN}.conf"; ENABLED=""
fi

say "Contrôles préalables"
nginx -t 2>&1 | tail -1
nginx -t >/dev/null 2>&1 || fail "La configuration nginx ACTUELLE est déjà invalide : je n'y touche pas."
OTHER="$(nginx -T 2>/dev/null | awk -v f="$AVAIL" -v g="${ENABLED:-//}" '/^# configuration file /{cur=$4; sub(":$","",cur)} cur!=f && cur!=g' \
         | grep -v '^\s*#' | grep -E "(^|[[:space:];{])server_name[^;]*[[:space:]]${DOMAIN//./\\.}([[:space:];]|$)" || true)"
[ -z "$OTHER" ] || fail "Un autre site déclare déjà « ${DOMAIN} » dans nginx : je ne le remplace pas.\n${OTHER}"
ok "nginx -t valide, aucun autre site ne déclare ${DOMAIN}"

# Codes HTTP des autres sites AVANT (pour prouver qu'ils n'ont pas changé)
NAMES="$(nginx -T 2>/dev/null | grep -v '^\s*#' | grep -oE '(^|[[:space:];{])server_name[^;]*' | tr -s ' \t;{' '\n' | grep -vE '^(server_name|_|localhost|)$' \
         | grep -v '[*~]' | grep -vx "$DOMAIN" | sort -u | head -25 || true)"
codes() { for n in $NAMES; do printf '%s %s\n' "$n" "$(curl --noproxy '*' -sk -o /dev/null -w '%{http_code}' --max-time 8 \
            --resolve "$n:443:127.0.0.1" "https://$n/")"; done; }
BEFORE="$(codes)"

say "Sauvegarde datée"
tar -czf "/root/nginx-sauvegarde-${STAMP}.tgz" -C / etc/nginx
ok "/root/nginx-sauvegarde-${STAMP}.tgz"

write_conf() {   # write_conf http|https
  local proxy="
    client_max_body_size ${BODY_MB}m;   # MAX_UPLOAD_MB de l'app + 10 % (en-têtes multipart)
    client_body_timeout 300s;         # tablette en 4G : pauses possibles pendant un gros envoi
    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_request_buffering off;  # gros PDF transmis au fil de l'eau (pas de copie disque côté nginx)
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;      # traduction / discussion sur CPU : jusqu'à plusieurs minutes
        proxy_send_timeout 600s;
    }"
  {
    echo "# Lecteur de livres audio — ajouté par vps/nginx-site.sh le ${STAMP}. Fichier propre au Lecteur."
    echo "server {"
    echo "    listen 80;"
    echo "    server_name ${DOMAIN};"
    echo "    location ^~ /.well-known/acme-challenge/ { root ${WEBROOT}; default_type text/plain; }"
    if [ "$1" = https ]; then echo "    location / { return 301 https://\$host\$request_uri; }"; else echo "$proxy"; fi
    echo "}"
    if [ "$1" = https ]; then
      echo "server {"
      echo "    listen 443 ssl;"
      echo "    server_name ${DOMAIN};"
      echo "    ssl_certificate ${CERT_DIR}/fullchain.pem;"
      echo "    ssl_certificate_key ${CERT_DIR}/privkey.pem;"
      echo "$proxy"
      echo "}"
    fi
  } > "$AVAIL.new"
  local prev=""
  if [ -f "$AVAIL" ]; then prev="$AVAIL.precedent.${STAMP}"; cp -a "$AVAIL" "$prev"; fi
  mv "$AVAIL.new" "$AVAIL"
  [ -n "$ENABLED" ] && ln -sfn "$AVAIL" "$ENABLED"
  if ! nginx -t 2>/tmp/nginx-t.log; then
    cat /tmp/nginx-t.log
    mv "$AVAIL" "$AVAIL.desactive.${STAMP}"
    if [ -n "$prev" ]; then
      cp -a "$prev" "$AVAIL"                                  # notre version précédente, qui fonctionnait
      fail "nginx -t refuse le nouveau bloc (mis de côté : $AVAIL.desactive.${STAMP}) ; version précédente remise, nginx NON rechargé."
    fi
    [ -n "$ENABLED" ] && rm -f "$ENABLED"                     # lien créé par nous à l'instant
    fail "nginx -t refuse notre bloc : mis de côté ($AVAIL.desactive.${STAMP}), nginx NON rechargé."
  fi
  reload_nginx
  ok "bloc $1 ajouté ($AVAIL), nginx -t valide, nginx rechargé"
}

say "Étape 1 : bloc HTTP (validation Let's Encrypt)"
mkdir -p "$WEBROOT/.well-known/acme-challenge"
if [ -s "$CERT_DIR/fullchain.pem" ]; then ok "certificat déjà présent : $CERT_DIR"
else
  write_conf http
  PROBE="lecteur-$STAMP"; echo "$PROBE" > "$WEBROOT/.well-known/acme-challenge/$PROBE"
  PUBIP="$(curl -4 -fs --max-time 5 https://api.ipify.org || true)"
  PUBIP="${PROBE_IP:-$PUBIP}"        # PROBE_IP : tests uniquement
  GOT="$(curl --noproxy '*' -s --max-time 10 --resolve "$DOMAIN:80:${PUBIP:-127.0.0.1}" "http://$DOMAIN/.well-known/acme-challenge/$PROBE" || true)"
  rm -f "$WEBROOT/.well-known/acme-challenge/$PROBE"
  [ "$GOT" = "$PROBE" ] || fail "http://$DOMAIN/.well-known/… ne renvoie pas notre fichier (reçu : ${GOT:0:80}). Un autre bloc capte le port 80 ?"
  ok "défi HTTP joignable depuis l'IP publique ${PUBIP:-?}"
  if [ "${SKIP_CERTBOT:-0}" != 1 ]; then
    command -v certbot >/dev/null || { DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot >/dev/null; }
    EM=(--register-unsafely-without-email); [ -n "${CERTBOT_EMAIL:-}" ] && EM=(-m "$CERTBOT_EMAIL")
    certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --agree-tos "${EM[@]}" \
      --deploy-hook "systemctl reload nginx" || fail "certbot n'a pas obtenu le certificat (le bloc HTTP reste en place)."
  fi
  [ -s "$CERT_DIR/fullchain.pem" ] || fail "certificat absent : $CERT_DIR/fullchain.pem"
  ok "certificat obtenu : $CERT_DIR"
fi

say "Étape 2 : bloc HTTPS"
write_conf https

say "Vérifications"
get() {   # get URL → "code|corps" ; HTTPS résolu localement (le DNS public pointe déjà ici)
  # shellcheck disable=SC2086
  curl --noproxy '*' -s --max-time 15 -w '|%{http_code}' --resolve "$DOMAIN:443:127.0.0.1" \
       --resolve "$DOMAIN:80:127.0.0.1" ${INSECURE:-} "$1"
}
# le rechargement de nginx est progressif : on attend que NOTRE bloc réponde (contenu du Lecteur, pas un autre site)
for _ in $(seq 1 15); do get "https://$DOMAIN/manifest.webmanifest" | grep -q '"name": "Lecteur de livres"' && break; sleep 1; done
check() {   # check URL code_attendu motif_attendu libellé
  local r; r="$(get "$1")"
  if [ "${r##*|}" = "$2" ] && printf '%s' "${r%|*}" | grep -q -- "$3"; then ok "$4 → $2 (contenu du Lecteur vérifié)"
  else fail "$4 → ${r##*|} ; contenu inattendu : $(printf '%s' "${r%|*}" | head -c 120)"; fi
}
check "https://$DOMAIN/" 200 '<title>Lecteur de livres' "https://$DOMAIN/"
check "https://$DOMAIN/manifest.webmanifest" 200 '"name": "Lecteur de livres"' "manifeste PWA"
check "https://$DOMAIN/sw.js" 200 'addEventListener' "service worker"
check "https://$DOMAIN/api/config" 401 'Token invalide' "API sans jeton"
r="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code} %{redirect_url}' --resolve "$DOMAIN:80:127.0.0.1" "http://$DOMAIN/")"
[ "$r" = "301 https://$DOMAIN/" ] && ok "http:// → 301 vers https://" || fail "http:// → $r"
AFTER="$(codes)"
if [ "$BEFORE" = "$AFTER" ]; then ok "autres sites : codes HTTP identiques avant/après ($(echo "$NAMES" | grep -c . || true) noms)"
else echo "AVANT :"; echo "$BEFORE"; echo "APRÈS :"; echo "$AFTER"; fail "un autre site répond différemment après l'ajout !"; fi
echo "Retour arrière si besoin : rm $AVAIL ${ENABLED} && nginx -t && systemctl reload nginx"
ok "Lecteur publié : https://$DOMAIN"
