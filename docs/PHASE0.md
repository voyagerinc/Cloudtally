# Phase 0 – connector proof against real TallyPrime

The PRD (§13, Appendix B) says not to commit to full implementation until the connector is proven on
a **disposable Tally test company**. The automated tests run against `tools/mock_tally.py`, which
copies Tally's XML protocol but is not Tally. This file records what has been verified on real
TallyPrime and what still has to be.

## Verified on the live TallyPrime (read-only, 1 Nov 2026)

Server: TallyPrime on `localhost:9000`, company *VIKAS SPOOL PRIVATE LIMITED – (from 1-Apr-26)*.
No data was written to Tally. Only Export (read) requests were sent.

| Check | Result |
|---|---|
| XML-over-HTTP listener reachable | ✅ `TallyPrime Server is Running` |
| Company list + GUID via Collection export | ✅ |
| Master export with `$AlterID` / `$Name` filters | ✅ 250 groups, 3,993 ledgers, 37 voucher types, 26 units, 16 godowns, 140 stock groups, 2,778 stock items, 1 cost centre |
| Masters import into PostgreSQL | ✅ 7,241 records, 0 failures, ~15 s; re-import is idempotent (all *unchanged*) |
| Stable identity | ✅ every object has `GUID` = company GUID + `-` + hex(`MASTERID`) and a company-wide increasing `ALTERID` |
| Opening balances | ✅ balance sheet balances (₹81,12,57,404.57 each side). Tally's own data has a ₹69,438.50 *difference in opening balances*, and the raw XML sums to the same figure |
| Ledger party details | ✅ address, PIN, country, GSTIN (from the dated GST list: 2,933 ledgers vs 1,649 in the flat field), contacts, party bank accounts, own bank details, MSME. All 3,993 ledgers import with 0 failures |
| Real-data quirks handled | `CMPINFO` count block reuses tag names; `&#4; Primary` parents; malformed 11-character PAN; 95-character ledger names; `Profit & Loss A/c` with no group; stock kept in Stock-in-Hand ledgers (non-integrated inventory); free text in the PIN code field |

### ⚠️ Finding: how Tally answers voucher requests on a large company (measured 1 Nov 2026)

The company has about 40,000 vouchers. Measured with single requests, one at a time:

| Request | Result |
|---|---|
| Company change counters (`ALTVCHID`, `ALTMSTID`) | **0.03 s** |
| All 3,993 ledgers with address, GST, contact, bank, MSME | 2.9 s |
| Vouchers with `SVFROMDATE`/`SVTODATE` = one day, 2 header fields | **all 40,281 vouchers**, 50 MB, 28 s: the date range is ignored |
| Any voucher filter that matches nothing (`$AlterID > N`, `$MasterID = N`) | ~15 s (Tally walks every voucher) |
| 66 full vouchers via `$MasterID >= a AND $MasterID <= b` | 18 s, 2.4 MB (≈37 KB per voucher) |
| Every voucher with its lines in one request (the original design) | never finished; Tally grew to 4 GB and its screen froze for many minutes |

A request the client has given up on keeps running inside Tally, and Tally answers one request at a
time, so a single oversized request freezes Tally for its users.

What the connector does because of this:

1. It reads the company change counters first and asks for nothing else if they have not moved.
2. It finds changed vouchers with one header-only scan (`$AlterID > watermark`), at most every
   `ERP_SYNC_VOUCHER_SCAN_SECONDS` (default 120), and only when the counter moved. *Sync now* forces one,
   and backlog passes do not wait.
3. It fetches full vouchers only for those changes, in MasterID-range batches of
   `ERP_SYNC_VOUCHER_BATCH_SIZE` (default 200), pausing `ERP_SYNC_VOUCHER_BATCH_PAUSE` seconds between them.
4. A backlog is read oldest change first, so the watermark moves up after every pass and each later scan
   is smaller.
5. Initial import and reconciliation use the same index-then-batches path and filter dates themselves.
   No code path asks Tally for unfiltered vouchers; the client refuses to.

A first full voucher import of this company is roughly 200 batches. Run it outside working hours.

## Must be proven on a disposable test company before outbound is enabled

Run these with the connector pointed at a **test** company (`outbound_enabled = true` only there):

| # | Behaviour | How the utility depends on it | Fallback if it fails |
|---|---|---|---|
| 1 | `ACTION="Create"` returns `LASTVCHID` (= new MasterID) | read-back by MasterID | read-back by `REMOTEID` is already implemented |
| 2 | `REMOTEID` set on Create is stored and filterable (`$RemoteID`) | idempotent retry after a lost response | match by type + number + date + content hash |
| 3 | `ACTION="Alter"` with `TAGNAME="MASTERID"` updates the **same** voucher | same-voucher update | switch connector `alter_by` to `guid` or `remoteid` |
| 4 | Alter **replaces** ledger/inventory lists rather than appending | every edit | read-back hash check flags `verify_mismatch`; then a TDL is needed |
| 5 | `ACTION="Cancel"` sets `ISCANCELLED=Yes` and keeps the number | cancel sync | — |
| 6 | Voucher types with *Automatic* numbering accept the ERP number | numbering preserved | set those types to *Manual* / *Automatic (Manual Override)*, or give ERP its own prefix |
| 7 | GST/e-invoice fields used by the pilot round-trip | statutory data | record in a field coverage register |
| 8 | Concurrent edit in Tally and ERP produces a conflict, not an overwrite | conflict policy | already enforced by ERP-side version/hash checks |
| 9 | Ledger Alter with address, contact, bank and MSME lists | ERP edits of party details reaching Tally | until proven, the ledger writer sends only name, group, opening, bill-wise, credit period, GSTIN, PAN, state and mailing name; the other party details are read from Tally only |

Every outbound write is **read back and hashed**. If Tally stores anything different from what the
ERP sent, the job fails with `verify_mismatch` and the field-level diff is shown in the sync
dashboard. It is never silently marked synced.
