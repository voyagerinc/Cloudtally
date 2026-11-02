#!/usr/bin/env bash
# Sets up Tally Cloud on a fresh Ubuntu 22.04 / 24.04 server (e.g. a Contabo VPS).
#
#   sudo bash deploy/server-setup.sh tallycloud.voyager.co.in admin@voyager.co.in
#
# Run it from the cloned repository, e.g. /root/projects/tallycloud. Safe to run again:
# it keeps an existing database password and .env, and only adds what is missing.
#
# What it installs: PostgreSQL, Python venv, the app as a systemd service on 127.0.0.1:8000,
# nginx in front with a Let's Encrypt certificate, a firewall (SSH + HTTPS only), and a locked-down
# "tallytunnel" account that Tally PCs use to open their reverse tunnel (see docs/DEPLOY.md).
set -euo pipefail

DOMAIN=${1:?usage: server-setup.sh <domain> <email for Let\'s Encrypt>}
EMAIL=${2:?usage: server-setup.sh <domain> <email for Let\'s Encrypt>}
APP_DIR=$(cd "$(dirname "$0")/.." && pwd -P)   # real path, also when started through a symlink
APP_USER=tallycloud
TUNNEL_USER=tallytunnel
ENV_FILE="$APP_DIR/backend/.env"

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)."; exit 1; }

echo "==> Packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq python3 python3-venv python3-pip postgresql nginx certbot \
  python3-certbot-nginx ufw openssl acl

echo "==> Accounts"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
id "$TUNNEL_USER" >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin "$TUNNEL_USER"
install -d -m 700 -o "$TUNNEL_USER" -g "$TUNNEL_USER" "/home/$TUNNEL_USER/.ssh"
touch "/home/$TUNNEL_USER/.ssh/authorized_keys"
chown "$TUNNEL_USER:$TUNNEL_USER" "/home/$TUNNEL_USER/.ssh/authorized_keys"
chmod 600 "/home/$TUNNEL_USER/.ssh/authorized_keys"

# The app's account must pass through the parent folders (e.g. /root, which only root may enter) to
# reach APP_DIR. Grant it "pass through" (x) only: it still cannot list or read anything else there.
d=$(dirname "$APP_DIR")
while [ "$d" != "/" ]; do
  sudo -u "$APP_USER" test -x "$d" || setfacl -m "u:$APP_USER:x" "$d"
  d=$(dirname "$d")
done

echo "==> Database"
systemctl enable --now postgresql
if [ -f "$ENV_FILE" ] && grep -q '^ERP_DATABASE_URL=' "$ENV_FILE"; then
  echo "    keeping the existing $ENV_FILE"
else
  DB_PASS=$(openssl rand -hex 24)
  if sudo -u postgres psql -tAc "select 1 from pg_roles where rolname='erp'" | grep -q 1; then
    sudo -u postgres psql -qc "alter role erp with login password '$DB_PASS'"
  else
    sudo -u postgres psql -qc "create role erp with login password '$DB_PASS'"
  fi
  sudo -u postgres psql -tAc "select 1 from pg_database where datname='tally_erp'" | grep -q 1 \
    || sudo -u postgres createdb -O erp tally_erp
  cat > "$ENV_FILE" <<EOF
# Created by deploy/server-setup.sh. Never commit this file.
ERP_DATABASE_URL=postgresql+psycopg://erp:$DB_PASS@127.0.0.1:5432/tally_erp
# Sync with every linked Tally automatically while it is reachable (Sync tab can switch it off).
ERP_AUTO_SYNC=true
ERP_TALLY_TIMEOUT=90
EOF
fi
chown root:"$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "==> Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"
chown -R root:"$APP_USER" "$APP_DIR"
chmod -R g+rX,o-rwx "$APP_DIR"

echo "==> Schema"
(cd "$APP_DIR/backend" && sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m app.cli init-db)

echo "==> Service"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__APP_USER__#$APP_USER#g" "$APP_DIR/deploy/tallycloud.service" \
  > /etc/systemd/system/tallycloud.service
systemctl daemon-reload
systemctl enable tallycloud
systemctl restart tallycloud

echo "==> nginx"
sed -e "s#__DOMAIN__#$DOMAIN#g" "$APP_DIR/deploy/nginx-tallycloud.conf" > /etc/nginx/sites-available/tallycloud
ln -sf /etc/nginx/sites-available/tallycloud /etc/nginx/sites-enabled/tallycloud
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> Firewall (SSH and web only; Tally is never exposed)"
ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
ufw --force enable >/dev/null

echo "==> HTTPS certificate"
if certbot certificates 2>/dev/null | grep -q "Domains: $DOMAIN"; then
  echo "    certificate already present"
else
  certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --redirect --non-interactive \
    || echo "!! certbot failed: check that the DNS A record of $DOMAIN points to this server, then run: certbot --nginx -d $DOMAIN"
fi

sleep 2
if curl -fsS http://127.0.0.1:8000/health >/dev/null; then echo "==> App is running"; else
  echo "!! The app did not answer; see: journalctl -u tallycloud -n 50"; fi
cat <<EOF

Done. Next:
  1. Create the first user (prints the sign-in key once):
       cd $APP_DIR/backend && sudo -u $APP_USER $APP_DIR/.venv/bin/python -m app.cli create-user admin --superadmin
  2. Connect each Tally PC with a tunnel (docs/DEPLOY.md, step 5):
       sudo bash $APP_DIR/deploy/add-tally-pc.sh "<public key from the Tally PC>" 9000
  3. Open https://$DOMAIN and sign in.
EOF
