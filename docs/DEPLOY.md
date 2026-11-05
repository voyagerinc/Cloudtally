# Deploying Tally Cloud on a VPS

Target: `https://tallycloud.voyager.co.in` on an Ubuntu 22.04 / 24.04 VPS (Contabo), code from
`git@github.com:voyagerinc/Cloudtally.git`.

## How the pieces connect

```
 Office PC (Windows)                               VPS  tallycloud.voyager.co.in
 ┌──────────────────────┐   SSH, outbound only    ┌───────────────────────────────────────┐
 │ TallyPrime :9000     │◄──────────────────────── │ 127.0.0.1:9000  (tunnel end)          │
 │ "Tally Cloud tunnel" │ ───────────────────────► │      ▲                                │
 │  scheduled task      │   reverse tunnel        │      │ connector host 127.0.0.1:9000  │
 └──────────────────────┘                          │ app (systemd, 127.0.0.1:8000)         │
                                                   │      ▲          PostgreSQL            │
 Browser ── HTTPS 443 ───────────────────────────► │ nginx + Let's Encrypt                 │
                                                   └───────────────────────────────────────┘
```

The cloud server cannot reach `localhost:9000` on an office PC, and Tally's XML port must never be
opened to the internet. So the Tally PC dials **out** to the server over SSH and forwards Tally to
the server's own `127.0.0.1:9000`. Nothing is opened on the office router. The tunnel account on the
server can do nothing except that one forward. Each further Tally PC gets its own port (9001, 9002, …).

## 1. DNS

Create an **A record** `tallycloud.voyager.co.in` → the VPS's public IP. Check it from the VPS:
`getent hosts tallycloud.voyager.co.in`.

## 2. Let the VPS read the GitHub repository (deploy key)

On the VPS, as root:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/cloudtally_deploy -C "tallycloud-vps"
cat ~/.ssh/cloudtally_deploy.pub
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/cloudtally_deploy
  IdentitiesOnly yes
EOF
```

On GitHub: **voyagerinc/Cloudtally → Settings → Deploy keys → Add deploy key**, paste the key, leave
"Allow write access" **off**. Keep the repository **private**.

## 3. Get the code

The app lives in `/root/projects/tallycloud`. The setup script lets the app's own service account pass
through `/root` and `/root/projects` to reach it, without being able to read anything else there.

```bash
cd /root/projects/tallycloud
git clone git@github.com:voyagerinc/Cloudtally.git .
```

## 4. Install

The server may already run other software. First see which ports are taken:

```bash
sudo ss -ltnp | awk 'NR>1 {print $4, $6}' | sort -u
```

The script shares the server safely:

- **Ports 80/443** must be free or held by nginx. If another web server (Apache, Caddy, Traefik,
  a Docker proxy) holds them, the script stops before changing anything.
- **nginx**: it adds only its own site for `tallycloud.voyager.co.in`. Other sites are left as they are.
- **App port**: it uses 127.0.0.1:8000, or the next free port. Set your own with `APP_PORT=8010 sudo -E bash …`.
- **PostgreSQL**: it adds a separate `erp` role and `tally_erp` database to the server's PostgreSQL,
  on whatever port that PostgreSQL uses.
- **Firewall**: if ufw is already on, it allows SSH, HTTP and HTTPS. If ufw is off, it leaves it off,
  so the other software keeps its ports.

```bash
sudo bash /root/projects/tallycloud/deploy/server-setup.sh tallycloud.voyager.co.in <your-email>
```

This installs PostgreSQL (with a generated password in `backend/.env`, readable only by the app),
the app as the `tallycloud` systemd service, and its nginx site with an HTTPS certificate. Then
create the first sign-in key:

```bash
cd /root/projects/tallycloud/backend
sudo -u tallycloud /root/projects/tallycloud/.venv/bin/python -m app.cli create-user admin --superadmin
```

The key is printed **once**. Store it in your password manager.

## 5. Connect the Tally PC

On the Windows PC that runs TallyPrime (TallyPrime → F1 Help → Settings → Connectivity: *Client/Server
configuration* → TallyPrime acts as **Server**, port **9000**). Copy `deploy\windows\setup-tally-tunnel.ps1`
from the repository to the PC and run:

```powershell
powershell -ExecutionPolicy Bypass -File setup-tally-tunnel.ps1
```

It prints one line starting with `sudo bash /root/projects/tallycloud/deploy/add-tally-pc.sh "ssh-ed25519 …" 9000`.
Run that line on the VPS. If other software on the VPS already uses port 9000 (Portainer often does),
`add-tally-pc.sh` refuses it. Then use a free port such as 9100 everywhere: `-RemotePort 9100` on the PC,
`… 9100` on the VPS, and port 9100 in the app's connector. Then, on the VPS:

```bash
curl -s http://127.0.0.1:9000        # <RESPONSE>TallyPrime Server is Running</RESPONSE>
```

A second Tally PC: run the script there with `-RemotePort 9001`, add its key with port `9001`, and use
port 9001 in that company's connector.

The tunnel runs as the scheduled task **Tally Cloud tunnel** whenever that Windows user is logged on,
and reconnects by itself. Its log is `%USERPROFILE%\.tallycloud\tunnel.log`.

## 6. Use it

Open `https://tallycloud.voyager.co.in`, sign in with the key, then **Open Tally company** with host
`127.0.0.1` and port `9000`. Link the company, then **Check Tally** on the Sync tab.

Automatic sync is on (`ERP_AUTO_SYNC=true` in `backend/.env`). A newly linked large company first
reads its whole voucher history, one slot of up to 1,000 vouchers after another, which keeps Tally busy;
switch automatic sync off on the Sync tab during office hours until that first catch-up is done.

## Updating

```bash
cd /root/projects/tallycloud && git pull
.venv/bin/pip install -q -r backend/requirements.txt
cd backend && sudo -u tallycloud ../.venv/bin/python -m app.cli init-db
sudo systemctl restart tallycloud
```

## Operating

| Task | Command |
|---|---|
| App log | `journalctl -u tallycloud -f` |
| Restart | `sudo systemctl restart tallycloud` |
| Is Tally reachable from the server? | `curl -s http://127.0.0.1:9000` |
| Nightly database backup | `sudo -u postgres pg_dump -Fc tally_erp > /var/backups/tally_erp_$(date +%F).dump` (put it in `/etc/cron.daily`, and copy it off the server) |
| Certificate renewal | automatic (`systemctl list-timers` shows `certbot.timer`) |

Only one app process may run (`--workers 1` in the service): automatic sync runs inside it.
