# Cloud licensing

How Tally Cloud implements *Cloud_Licensing_Integration_PRD(2).docx*. Licensing is an addition: the
accounting, reports and Tally sync code is unchanged apart from one licence check where permissions
were already checked.

## Decisions taken (PRD §15)

| Question | Decision |
|---|---|
| Customer installation target | **Windows**, next to Tally (no SSH tunnel needed on the LAN) |
| User limit | **Named users**: switched-on users who sign in with a password |
| After expiry, suspension or 7 days offline | **Read-only**: sign in, view, reports, export and backup work; no new or changed vouchers or masters, and no Tally sync. Data is never deleted. |
| Hosted Tally Cloud | **Continues.** tallycloud.voyager.co.in is the licence portal, and books hosted there that belong to a customer follow that customer's licence. The owner's own books are not limited. |
| Licensed per | Customer, with limits on named users, servers (installations), Tally companies, modules and versions |
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

1. **Super admin** (Customers tab) adds a customer, then a **company admin**. The admin gets a
   one-time link (72 hours) and sets their own password; no password is sent by e-mail.
2. **Super admin** creates a licence with plan (trial, monthly, annual, perpetual), dates, named
   users, servers, Tally companies, modules, version range, offline grace (7 days) and check-in
   interval (24 hours). The key (`TCL-XXXX-…`) is shown **once**. Only its hash is stored.
3. **Company admin** signs in and sees only their own customer: licence, servers, warnings and the
   installers their licence covers. They can download them and ask for renewal.
4. On the installed server, the super admin enters the key on the **Licence** tab. The server sends
   the key, a random installation id made at first start (not hardware based), its name and version.
   The cloud checks the customer, status, dates, version and server limit. It then returns an
   **Ed25519-signed entitlement** and a secret for this installation. Later check-ins use that secret,
   not the key.
5. The installation checks in every 24 hours. Licence changes (renewal, suspension, modules, limits)
   arrive with the next check-in. If the cloud can't be reached, everything keeps working for the
   grace period, with a warning. After that the server goes read-only until it connects again. A
   clock set back does not extend the grace period.
6. **Moving server:** Licence → Deactivate on the old server gives its slot back. If the old server
   is gone, the super admin uses **Release slot**. **Block** stops a server from ever activating again.

## What each PRD requirement maps to

| PRD | Where |
|---|---|
| FR-01, 02 customers and company admins, invitations, reset, disable | `app/api/licensing.py` (admin/customers, admin/users), `app/licensing/service.py` (invites) |
| FR-03, 04 licences, key, plans | `app/api/licensing.py` (admin/licenses), `app/licensing/core.py` |
| FR-05 customer portal | `GET /portal/license`, *My licence* tab |
| FR-06 download authorisation | `POST /downloads/{id}/link`: a 5-minute link, then `GET /downloads/{id}`; every attempt is logged in `download_events` |
| FR-08, 09 activation, installation identity, rehost | `POST /licenses/activate`, `/deactivate`, release, block |
| FR-10 entitlements | `app/licensing/enforce.py`: modules, read-only, named users, Tally companies, versions |
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

## Setting up an installation by hand (until the Windows installer exists)

In that server's `backend/.env`:

```
ERP_LICENSE_MODE=client
ERP_LICENSE_PUBLIC_KEY=<public key from the portal>
ERP_LICENSE_SERVER_URL=https://tallycloud.voyager.co.in
```

Start the app, sign in as its super admin, open **Licence**, and enter the licence key.

## Migration and rollback

* Migrations `0008` (passwords, sessions) and `0009` (licensing) only add tables and columns. They do
  not change existing data.
* To switch licensing off: set `ERP_LICENSE_MODE=off` and restart. Every check stops at once, and the
  licensing tables stay in place.
* Full rollback: `alembic downgrade 0008`, then deploy the previous commit. Take a database backup first
  (`pg_dump`, see DEPLOY.md).

## Known limitations

* **The Windows installer itself (FR-07) is not built yet.** The portal already stores, signs and
  serves installer files. Packaging the app with Python, PostgreSQL, a Windows service and the public
  key, and the data-preserving upgrade, is the next phase. Until then, installations are set up by
  hand as shown above.
* **Updates on an installation** are shown, not installed: the company admin downloads the new
  installer from the portal.
* **MFA** (optional in the PRD) is not implemented.
* **E-mail** is implemented with standard SMTP but has not been tested against a real mail server.
* **Rate limits** on activation and invitation endpoints are kept in memory, per app process. That
  matches the single-process service.
* Modules are stored as a list on the licence, not in a separate `license_modules` table. Limits
  per module can be added when one is needed.
* **Payments** are outside the software (PRD out of scope). *Request renewal* records the request and
  tells Voyager.
