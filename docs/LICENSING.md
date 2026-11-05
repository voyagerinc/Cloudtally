# Cloud licensing

How Tally Cloud implements *Cloud_Licensing_Integration_PRD(2).docx*. Licensing is an addition: the
accounting, reports and Tally sync code is unchanged apart from one licence check where permissions
were already checked.

## Decisions taken (PRD §15)

| Question | Decision |
|---|---|
| Customer installation target | **Windows**, next to Tally (no SSH tunnel needed on the LAN) |
| User limit | **Users at the same time.** A licence for 5 users lets 5 people work at once. Any number of user IDs can exist. A 6th person can sign in when someone signs out, or 15 minutes after a browser was last active. One person in two browsers takes one seat. Super admins and API-key integrations take no seat. A company admin can sign someone out to free a seat at once. |
| After expiry, suspension or 7 days offline | **Read-only**: sign in, view, reports, export and backup work; no new or changed vouchers or masters, and no Tally sync. Data is never deleted. |
| Hosted Tally Cloud | **Continues.** tallycloud.voyager.co.in is the licence portal, and books hosted there that belong to a customer follow that customer's licence. The owner's own books are not limited. |
| Licensed per | Customer, with limits on users at the same time, servers (installations), Tally companies, modules and versions |
| Updates | The company admin downloads releases the licence covers; publishing is the super admin's decision |

"Company" in the PRD means the licensee. In the code it is a **customer** (`customers`), because
`companies` already means the books of one Tally company. One customer can have several companies.

## Three modes (`ERP_LICENSE_MODE`)

| Mode | Where | What it does |
|---|---|---|
| `server` | tallycloud.voyager.co.in | Licence portal: customers, licences, activation, heartbeat, releases, downloads. Hosted books of a customer follow its licence. |
| `client` | A customer's Windows server | Activates with the licence key, checks in daily, and enforces the signed entitlement. |
| `off` | Development, tests | No licence checks (the default). |

## How it works

1. **Super admin → Customers → Add customer.** One form holds the customer's details, the licence
   (**number of users working at the same time**, plan, dates, servers, Tally companies) and the
   **customer admin's user ID** (the manager). Saving shows the **licence key** (once) and the admin's
   **one-time link** (72 hours). Nothing is half-created: if the admin's user ID is taken, no customer is
   made.
2. **The customer admin** opens the link and chooses their own user ID (prefilled, changeable) and
   password. No password is ever sent.
3. **The customer admin signs in to the portal** (tallycloud.voyager.co.in → My licence). They see only
   their own licence, servers and warnings, and **download** the installers their licence covers.
4. **They install the software on their server or computer** and open its local address. The first
   time, a **setup page** asks for the licence key and creates the **manager** of that server (user ID
   and password). The server activates against the cloud: it sends the key, a random installation id
   made at first start (not hardware based), its name and version. It gets back an Ed25519-signed
   entitlement and its own secret for later check-ins. The setup page works only once, while no user
   exists, and only with a valid key.
5. **The manager** links the Tally company and creates the other users on the Users tab, with their
   own IDs and passwords. **Everyone else signs in at the same local address** and works with the data.
   **Only the manager (company admin role) runs Tally sync and changes the Tally connection.** Other
   users can be allowed to view the sync status.
6. **Users at the same time:** a licence for 5 users lets 5 people work at once; any number of user IDs
   can exist. A seat frees on sign-out or 15 minutes after a browser was last active. The manager can
   sign someone out to free a seat at once.
7. The server checks in every 24 hours. Licence changes (renewal, suspension, users, modules) arrive
   with the next check-in. If the cloud can't be reached, everything keeps working for 7 days, with a
   warning. After that it is read-only until it connects again. A clock set back does not extend this.
8. **Moving server:** Licence → Deactivate on the old server gives its slot back. If the old server is
   gone, the super admin uses **Release slot**. **Block** stops a server from ever activating again.

## What each PRD requirement maps to

| PRD | Where |
|---|---|
| FR-01, 02 customers and company admins, invitations, reset, disable | `app/api/licensing.py` (admin/customers, admin/users), `app/licensing/service.py` (invites) |
| FR-03, 04 licences, key, plans | `app/api/licensing.py` (admin/licenses), `app/licensing/core.py` |
| FR-05 customer portal | `GET /portal/license`, *My licence* tab |
| FR-06 download authorisation | `POST /downloads/{id}/link`: a 5-minute link, then `GET /downloads/{id}`; every attempt is logged in `download_events` |
| FR-08, 09 activation, installation identity, rehost | `POST /licenses/activate`, `/deactivate`, release, block |
| FR-10 entitlements | `app/licensing/enforce.py`: modules, read-only, users at the same time (checked at sign-in, and again when an idle browser comes back), Tally companies, versions |
| FR-11, 12 heartbeat, offline grace | `POST /licenses/heartbeat`, `app/licensing/client.py` (hourly wake-up, check-in when due) |
| FR-13 central actions | activate, suspend, resume, revoke, cancel, renew, new key, release or block a server |
| FR-14 expiry | Warnings at 30/15/7/1 days (banner, portal, e-mail if SMTP is set), then read-only |
| FR-15 updates | Releases with SHA-256 and Ed25519 signature; installations see newer permitted versions on the Licence tab |
| FR-16 audit | Admin actions in the `audit_events` trail. Activation, heartbeat and admin events in `license_events`. Both are append-only (database trigger). |
| FR-17 reports | Customers tab: counts by status, expiring in 30 days, servers, versions, failed activations, downloads, renewal requests |
| FR-18 notifications | In the app (banner, portal); e-mail when `ERP_SMTP_*` is set |
| FR-19 tenant isolation | The portal uses the signed-in user's own customer and ignores ids sent by the browser. Tests: `tests/test_licensing.py` |
| FR-20 compatibility | One call in `authorize()`, a check before adding users or companies, and a check before automatic sync |

## Setting up the licence portal (tallycloud.voyager.co.in)

`deploy/server-setup.sh` does it (skip with `PORTAL=no`):

* It creates the signing key once, at `/etc/tallycloud/license_signing.pem` (readable by root and the
  app only). It is never in git, an installer, or a log. Back it up somewhere safe: if it is lost,
  every installed server must be activated again with a new public key.
* It adds `ERP_LICENSE_MODE=server`, the key file, `ERP_RELEASE_DIR=/var/lib/tallycloud/releases` and
  `ERP_PUBLIC_URL` to `backend/.env`.
* E-mail (optional) uses `ERP_SMTP_HOST`, `ERP_SMTP_PORT`, `ERP_SMTP_USER`, `ERP_SMTP_PASSWORD` and
  `ERP_SMTP_FROM`. Without them, the portal shows invitation links for you to send.

The public key that installations need:

```bash
curl -s https://tallycloud.voyager.co.in/api/v1/licensing/public-key
```

## Releases and versions

Every update is a release with a **higher version number**. The version lives in one place,
`backend/app/__init__.py`. Releases reach the portal **automatically through GitHub**.

1. **On the development PC**, from committed code:
   ```
   python deploy/make_release.py --bump patch      # fixes:        1.0.0 -> 1.0.1
   python deploy/make_release.py --bump minor      # new features: 1.0.1 -> 1.1.0
   git push origin main v1.0.1
   ```
   `--bump` writes the new version, commits it and tags the commit `v1.0.1`.
2. **GitHub Actions** (`.github/workflows/release.yml`) starts when the tag arrives:
   * it checks that the tag is the version in the code;
   * it builds `TallyCloud-1.0.1.zip` (holding `VERSION`, `install.cmd`, `README.txt` and the app);
   * it creates a **GitHub Release** with the package and notes made from the commit messages;
   * it **uploads the package to the portal**.

   The upload arrives as a draft, or published when the repository variable
   `TALLYCLOUD_AUTO_PUBLISH` is `true`.
3. **The portal** refuses a version that is not higher than every earlier release, and a package whose
   `VERSION` file says something else. It signs every file (SHA-256 plus an Ed25519 signature).
   On the **Releases** tab, check a draft and **Publish** it. Only licences whose version range covers
   a release can see it.
4. **The customer's server admin** sees it in two places:
   * **Portal → My licence → Downloads**: the zip, for a first installation.
   * **Their server → Licence → Check for updates**: the server downloads the release with its own
     credentials. It checks the signature first and the SHA-256 after downloading, and refuses a file
     that fails either check. Then **Install update and restart** runs `update-server.ps1`. That script
     keeps settings and data, swaps the app folder, installs packages, updates the database, and
     restarts. If the new version does not start, it puts the previous one back. Log:
     `C:\TallyCloud\logs\update.log`.

### One-time setup of the automatic release

1. On the portal server, make the release token. It writes only a hash to `.env`:
   ```bash
   cd ~/projects/tallycloud/backend
   sudo ../.venv/bin/python -m app.cli release-token
   sudo systemctl restart tallycloud
   ```
2. On GitHub, open **voyagerinc/Cloudtally → Settings → Secrets and variables → Actions** and add:
   * **secret** `TALLYCLOUD_RELEASE_TOKEN`: the token printed in step 1. It can only upload releases.
   * **variable** `TALLYCLOUD_PUBLIC_KEY` (optional): the output of
     `curl -s https://tallycloud.voyager.co.in/api/v1/licensing/public-key`, so the installer carries
     the key.
   * **variable** `TALLYCLOUD_AUTO_PUBLISH` (optional): `true` to publish without checking first.
   * **variable** `TALLYCLOUD_PORTAL_URL` (optional): the portal address, if it is not
     tallycloud.voyager.co.in.

Without the secret, the workflow still builds the package and attaches it to the GitHub Release; it
only skips the upload. A package can always be uploaded by hand on the Releases tab.

## First installation on a customer's Windows server or PC

Unpack the release zip, then right-click `install.cmd` → **Run as administrator**. The script
(`deploy/windows/install-server.ps1`):

* installs Python 3.12 and PostgreSQL 16 with winget if they are missing;
* makes a separate database with a random password, kept in `C:\TallyCloud\secrets` (readable by
  administrators only);
* installs the app in `C:\TallyCloud\app` and starts it with Windows (task *Tally Cloud*, port 8080);
* opens port 8080 for the office network only (Domain and Private firewall profiles).

The browser then opens the **first-run page**: licence key, and the manager's user ID and password.
Other users open `http://<computer name>:8080`. Running the installer again repairs the installation and
keeps its data.

## Setting up an installation by hand (without the installer)

In that server's `backend/.env`:

```
ERP_LICENSE_MODE=client
ERP_LICENSE_PUBLIC_KEY=<public key from the portal>
ERP_LICENSE_SERVER_URL=https://tallycloud.voyager.co.in
```

Start the app and open it. The first-run page asks for the licence key and creates the manager.

## Migration and rollback

* Migrations `0008` (passwords, sessions) and `0009` (licensing) only add tables and columns. They do
  not change existing data.
* To switch licensing off: set `ERP_LICENSE_MODE=off` and restart. Every check stops at once, and the
  licensing tables stay in place.
* Full rollback: `alembic downgrade 0008`, then deploy the previous commit. Take a database backup first
  (`pg_dump`, see DEPLOY.md).

## Known limitations

* **The Windows install and update scripts have not been run on a clean Windows machine yet.** Try them
  on a test PC before the first customer. The package is a signed zip with `install.cmd`, not a
  signed `.exe`. Code-signing an `.exe` needs a code-signing certificate.
* The office network reaches the app over plain HTTP on port 8080 (LAN only; the firewall rule
  excludes public networks). For HTTPS inside the office, put a reverse proxy with a certificate in
  front of it.
* **MFA** (optional in the PRD) is not implemented.
* **E-mail** is implemented with standard SMTP but has not been tested against a real mail server.
* **Rate limits** on activation and invitation endpoints are kept in memory, per app process. That
  matches the single-process service.
* Modules are stored as a list on the licence, not in a separate `license_modules` table. Limits
  per module can be added when one is needed.
* **Payments** are outside the software (PRD out of scope). *Request renewal* records the request and
  tells Voyager.
