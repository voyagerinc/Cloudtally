from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Override with ERP_* environment variables or backend/.env."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="ERP_", extra="ignore")

    database_url: str = "postgresql+psycopg://erp@localhost:55432/tally_erp"

    # Background sync worker
    sync_poll_seconds: int = 15            # how often the worker wakes up
    sync_inbound_overlap: int = 50         # re-read this many AlterIDs below the watermark
    # At most one voucher change scan per company this often (a scan costs a large Tally ~15 s); an
    # amendment made in Tally reaches the ERP within this time. Only runs when Tally's counter moved.
    sync_voucher_scan_seconds: int = 120
    sync_voucher_batch_size: int = 200     # full vouchers fetched from Tally per request
    sync_voucher_batch_pause: float = 2.0  # seconds between those requests, so Tally stays usable
    sync_voucher_max_batches: int = 5      # per sync pass; a larger backlog continues on the next passes
    auto_sync: bool = True                 # the API server syncs every company on its own while Tally is open
    sync_backlog_pause: float = 5.0        # seconds between passes while a backlog is being worked through
    sync_batch_size: int = 50
    sync_max_attempts: int = 8             # transient retries before routing to operator queue
    sync_backoff_base: float = 5.0         # seconds; exponential backoff with full jitter
    sync_backoff_cap: float = 900.0

    # Tally connector
    tally_timeout: float = 60.0
    session_idle_hours: float = 12.0       # a signed-in browser is signed out after this long without use
    session_max_days: float = 30.0         # ... and after this long in any case
    seat_idle_minutes: int = 15            # a licence seat is freed this long after the browser was last active
    login_max_failures: int = 5            # wrong passwords in a row before the user ID is locked
    login_lock_minutes: int = 15
    # Folder shared with deploy/update.sh (set by deploy/server-setup.sh). Unset: no updates from the web.
    update_dir: str | None = None

    # Cloud licensing (Cloud Licensing PRD). "off": no licence checks (development, tests).
    # "server": this is the licence portal (tallycloud.voyager.co.in); books hosted here that belong to a
    #           customer follow that customer's licence. "client": an installation on a customer's server.
    license_mode: str = "off"
    license_signing_key_file: str | None = None    # server: Ed25519 private key (PEM), never in git
    license_public_key: str | None = None          # client: base64 public key that checks the cloud's signature
    license_server_url: str = "https://tallycloud.voyager.co.in"
    install_dir: str | None = None                 # client: where deploy/windows/install-server.ps1 installed it
    release_dir: str | None = None                 # server: where uploaded installers are kept
    release_token_hash: str | None = None          # server: GitHub Actions' upload token (python -m app.cli release-token)
    public_url: str = "https://tallycloud.voyager.co.in"   # for links in invitations
    # Optional e-mail for invitations and licence notices; without it the portal shows the link to copy.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None

    # Voucher engine
    allow_negative_stock: bool = True


settings = Settings()
