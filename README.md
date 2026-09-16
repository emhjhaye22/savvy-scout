# TenderSight

Phase A: the core rule-based triage engine. Sweeps Find a Tender and
Contracts Finder, dedupes against what's already known, runs the five-gate
Phase 1 triage, and exports a multi-sheet Excel tracker.

Phase B: the workflow and judgement layer on top. A Flask dashboard for the
four named accounts, Claude-based Phase 2 scope reads, Victoria escalation
with an auto-drafted brief, and a bare-bones learning loop (admin tab).
Phase C (document harvest, calendar push, Teams nudges, Microsoft Lists
sync, the full admin tab) isn't built yet.

## Setup

1. Install Python 3.11 or later.
2. Create a virtual environment and install dependencies:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env`. The Find a Tender and Contracts Finder OCDS
   endpoints are public and need no key, so `sweep`/`triage`/`export`/
   `regression-test`/`backup` work with just the defaults. To use the
   dashboard's Approve action (B2) you also need `ANTHROPIC_API_KEY` and
   `FLASK_SECRET_KEY`; to send escalation emails from the dashboard (B3) you
   additionally need the Microsoft Graph settings, see below. Never commit
   `.env` or put a secret in code.
4. Initialise the database (creates `savvy_scout.db` and seeds every config
   table from the values below):
   ```
   python -m savvy_scout.cli init-db
   ```
5. Create a dashboard login for each of the four accounts (prompts for a
   password, never pass it as a CLI argument):
   ```
   python -m savvy_scout.cli create-user --username mark --display-name Mark
   python -m savvy_scout.cli create-user --username kanvesh --display-name Kanvesh
   python -m savvy_scout.cli create-user --username hammad --display-name Hammad
   python -m savvy_scout.cli create-user --username victoria --display-name Victoria
   ```
6. Run the dashboard:
   ```
   python -m savvy_scout.cli dashboard --port 5000
   ```
   Then open `http://127.0.0.1:5000`.

## Running it

```
python -m savvy_scout.cli sweep                                          # pull, dedupe, triage
python -m savvy_scout.cli triage                                         # re-triage anything still NEW, without sweeping
python -m savvy_scout.cli export --output tracker.xlsx                   # multi-sheet tracker
python -m savvy_scout.cli regression-test --baseline baseline.xlsx --output diff.xlsx
python -m savvy_scout.cli backup --backup-dir backups                    # daily DB backup
python -m savvy_scout.cli export-victoria-package --output-root "C:\path\to\01. Reports to Victoria"
```

`regression-test` exits with status 1 if there is any disagreement, so it
can be used as a pass/fail gate in a scheduled task or CI-style check.

### Victoria reports package

`export-victoria-package` uses the supplied house samples packaged under
`savvy_scout/templates/artifacts/` and writes a complete local package:

- one numbered folder per owner-approved Phase 2 opportunity, containing an
   Internal Addendum `.docx`, Capture Brief `.docx`, and original-notice `.pdf`;
- the supplied 19-column Trifork workbook with its formulas, validations and
   Pass/Flag/Fail sheets preserved, keyed by the published notice reference;
- weekly and monthly Word reports in their own folders.

Only genuine owner decisions by Mark, Kanvesh or Hammad are eligible. The
tracker and artifact package require a recorded Phase 2 assessment; system
cleanup rows and Phase 1 rejections are excluded. Re-running the command
updates existing reference rows and overwrites the same artifact filenames,
so it does not duplicate opportunities.

For automatic tracker refresh when the app runs on the same machine, set
`TRIFORK_PIPELINE_OUTPUT_PATH` to the generated workbook's absolute path.
A Render service cannot access a Windows `C:\Users\...` path. Cloud-hosted
automatic sync will require a Microsoft Lists/SharePoint target and Graph
`Sites.ReadWrite.All`; Graph mail continues to require `Mail.Send`.

### Windows Task Scheduler

Daily sweep and daily backup should run unattended. Example, from an
elevated prompt (adjust the venv and project paths):

```
schtasks /Create /TN "TenderSightSweep" /TR "C:\path\to\.venv\Scripts\python.exe -m savvy_scout.cli sweep" /SC DAILY /ST 06:00 /RU SYSTEM
schtasks /Create /TN "TenderSightBackup" /TR "C:\path\to\.venv\Scripts\python.exe -m savvy_scout.cli backup" /SC DAILY /ST 06:30 /RU SYSTEM
```

Set the task's "Start in" directory to the project root so the default
relative `.env` and `savvy_scout.db` paths resolve correctly, or set
`SAVVY_SCOUT_DB_PATH` to an absolute path.

### Microsoft Graph app registration (needed for B3's escalation email)

The escalation brief (`.docx`) generates and saves to `briefs/` without any
Graph setup at all; only the "Send escalation email" dashboard button (only
visible to Victoria) needs this. Register an Azure AD app with an
**application** permission for `Mail.Send` (admin-consented), grant it
send-as rights on the mailbox you'll send from, then set in `.env`:

```
MS_GRAPH_TENANT_ID=...
MS_GRAPH_CLIENT_ID=...
MS_GRAPH_CLIENT_SECRET=...
MS_GRAPH_SENDER_UPN=...   # an @bidsavvy.io mailbox, e.g. a shared/service account
```

`savvy_scout/graph/mail.py` hard-gates every send behind
`assert_whitelisted()` (recipient must be `@bidsavvy.io`, SPEC.md
non-negotiable 2), checked in code before any network call, not just in the
UI. Phase C (calendar push, Teams nudges, Lists sync) will need broader
Graph permissions (Calendars.ReadWrite, ChannelMessage.Send, Sites/Lists
read-write) later; not needed yet.

## Config layer

Every tunable rule value lives in the database, not in code, seeded once by
`init-db` (`savvy_scout/db/seed_config.py`) and left alone on later runs so a
manual correction is never overwritten. There's no admin UI yet (that's
Phase C5); edit these tables directly with `sqlite3 savvy_scout.db` or a
short script until then:

| Table | Governs |
|---|---|
| `config_owner_map` | Sector to owner (Gate 1) |
| `config_sector_keywords` | Buyer/text keyword to sector classification |
| `config_gate2_terms` | Gate 2 pass/fail/generic-needs-coupling terms |
| `config_coupling_terms` | Sector/capability terms that satisfy Gate 2 coupling |
| `config_framework_keywords` | Gate 3 call-off/establishment/direct language |
| `config_trifork_frameworks` | Frameworks Trifork is confirmed on (empty today) |
| `config_cpv_lists` | Gate 5 CPV pass/inferred/flag/fail lists |
| `config_scale_filter` | Filter 3 threshold, SI-prime list, enabled toggle |
| `config_capability_profile` | Trifork capability profile fed to the B2 scope read |

Phase B adds a bare-bones admin tab (`/admin`, Victoria and Kanvesh only)
that edits these tables from the dashboard instead of `sqlite3` directly, and
logs every change to `rule_corrections`, SPEC.md B4's learning loop. The full
admin tab (source tiers, keyword lists, email whitelist, Lists field
mapping) is Phase C5, not built yet.

## Decisions on record (2026-07-19)

You resolved the nine flagged disagreements between SPEC.md and the
reference docs as follows; the code implements these, not the SPEC.md
draft where the two differed:

1. Five-gate model (SPEC.md), not the references' seven-gate model. Canonical
   model stays open for Victoria.
2. Non-negotiable 3 reworded to "no minimum value floor ever." Filter 3
   (£500m + global SI-prime dominance) is a separately agreed rule, dated
   15 June 2026, active by default, config-driven with an on/off toggle
   (`config_scale_filter.enabled`).
3. Energy sector owner is Mark, not Kanvesh.
4. Gate 2: platform/digital/data require a sector or capability/product
   coupling term to PASS; uncoupled generic language FLAGs, it doesn't
   auto-pass. Fail list unchanged.
5. Maddy left the team 15 July 2026; her routing in the references is
   retired. Ambiguous or contested sector/ownership always FLAGs to
   Victoria. Rail and Transport is Mark's.
6. `Trifork_Scouting_Project_Instructions_2.md` is the current reference;
   the other two copies are superseded. Victoria is the sole authority for
   any threshold.
7. CPV lists are sourced from the Kanvesh scouting skill Section 4, verified
   against Home Office SCBP notice 039639-2026. That source lives outside
   this repo; `config_cpv_lists` is the copy of record here.
8. Gate 5 is judged on the primary CPV code
   (`tender.classification`, falling back to the first item's classification,
   falling back to the first `additionalClassifications` entry when neither
   is present, marked `[inferred]`). Additional CPV codes are recorded for
   reference and noted only when they conflict with the primary outcome. All
   six gates (five gates plus Filter 3) always run and record a result; there
   is no short-circuit. The first non-PASS result, in gate order, is the
   headline outcome.
9. The baseline tracker Excel for the manual 129-notice sweep will be added
   to the project folder later; A5's CLI command is built and tested against
   a synthetic baseline, but hasn't been run against the real sweep yet.

### Phase B gaps (flagged, not decided)

10. The references' Step 4 describes a fuller, later-stage capture package
    (a "Client Capture Brief" plus an "Internal Addendum") than SPEC.md's B3
    ten-section brief, built only after a deep-verification stage SPEC.md
    doesn't scope anywhere across its three phases. B3 is built exactly as
    SPEC.md specifies: one internal ten-section brief, Victoria-only. See
    "Open questions for Victoria" below.
11. Who besides Victoria can save a rule correction in B4's admin tab isn't
    specified by SPEC.md. Built so Victoria and Kanvesh both can (Kanvesh as
    the references' named process owner); the other two accounts can view
    every queue but not save a correction. Flagged for confirmation.
12. Per your cost/quality call, B2 uses `claude-sonnet-5`, not the Anthropic
    default `claude-opus-4-8`: near-Opus quality on this structured
    judgement task per Anthropic's own guidance, at roughly half the
    per-notice cost, given it runs on every Phase 1 approval.
13. **Gate 1 correction, found via a real sweep (2026-07-19).** A live sweep
    against Find a Tender showed Gate 1 originally FLAGged (and, with B3's
    auto-escalation wired up, immediately escalated to Victoria) any notice
    where no sector keyword matched at all, which is most real notices,
    since Find a Tender covers all UK public procurement, not just Trifork's
    sectors. Per your confirmation, this is now a FAIL ("obviously out of
    scope"), matching the references' Step 1 elimination rule; FLAG is
    reserved for when more than one sector's keywords match (genuinely
    contested). `gate1_sector_owner` in `triage/gates.py` and its tests were
    updated accordingly. A follow-on observation from the same sweep: Gate 2
    still FLAGs a fair number of real notices (e.g. council taxi-transport
    contracts) that are obviously non-technology work but don't match any of
    SPEC.md's four named Gate 2 fail terms (hardware, packaged product
    resale, managed service, resale). That's SPEC-compliant behavior
    ("never fail if unsure"), not a bug, but it means Victoria's queue will
    run noisier than ideal until the confirmed keyword/buyer lists (open
    questions 2 and 9) land.

## Open questions for Victoria

Carried over from SPEC.md, plus new ones from this build, kept visible here
rather than resolved in code:

1. Canonical gate model across all three desks (five-gate assumed here).
2. Confirmed buyer lists for Fintech, Aviation and Energy (currently draft).
3. 48xxx CPV ruling where Trifork is the product vendor (Corax, Tiris).
4. Whether this tool's output feeds or replaces the Microsoft Lists tracker
   as record.
5. Approval that auto-emailed escalation briefs to her are wanted at this
   cadence.
6. Formal confirmation that Energy sector ownership sits with Mark, and its
   removal from Kanvesh's scope in whatever document is the team's system
   of record.
7. Whether ambiguous aviation/rail routing (previously split between Maddy
   and Kanvesh coordination in the references) should permanently escalate
   to Victoria as its only path now that Maddy has left, or whether some
   ambiguous cases should route to Kanvesh instead.
8. Whether the fuller two-artifact capture package described in the
   references' Step 4 (Client Capture Brief plus Internal Addendum) is
   wanted as future work, since nothing in SPEC.md's three phases builds it.
9. Whether Kanvesh should have rule-correction authority in B4's admin tab
   alongside Victoria, or whether that should be Victoria-only.

## Implementation notes (heuristics worth knowing about)

Several things aren't specified by SPEC.md or the references at the level
of detail code needs, and were filled in with a documented, low-stakes,
easily-corrected default rather than paused on:

- **Buyer-to-sector classification** (feeds Gate 1 and the expiry radar) is
  keyword-based against `config_sector_keywords`, pending the confirmed
  buyer lists in open question 2. More than one sector's keywords matching
  is treated as contested and FLAGs.
- **Gate 3 framework detection** is keyword-based against
  `config_framework_keywords`, since no structured framework field exists on
  most notices. Any detected call-off fails unless the named framework
  appears in `config_trifork_frameworks`, which is empty today (no confirmed
  UK framework access as of the references, G-Cloud 15 in progress).
- **Excel tracker sheet population** (which rows land on "Phase 1 - Flags"
  vs "To review" vs "Handoffs") is Phase A's working split, documented in
  full in `savvy_scout/export/excel_tracker.py` and on the workbook's own
  "Legend and method" sheet. "Handoffs - `<owner>`" sheets are generated one
  per owner that actually appears in the data, not hardcoded to Mark,
  Kanvesh and Hammad.
- **Contract expiry review date** (A2 expiry radar) defaults to six months
  before the contract end date; SPEC.md doesn't specify a lead time.
- **The notice status transition graph** beyond SPEC.md's main line (when
  REJECTED/PARKED/MONITOR are reachable, and whether they can return to the
  main line) is Phase A's working assumption, see the module docstring in
  `savvy_scout/models/notice.py`, extended in Phase B where the real
  workflow needed it: `AWAITING_PHASE1_APPROVAL -> ESCALATED_TO_VICTORIA` is
  now a direct edge, since B3 escalates on a Gate 1/2/5 FLAG recorded at
  Phase 1 triage time, before Phase 2 scoping exists.
- **Two escalation triggers, both in `workflow/approvals.py`**: automatic
  (any gate FLAG/MAYBE escalates immediately after Phase 1 triage, in
  `sweep/runner.py::triage_pending`, before an owner ever sees the notice in
  their queue) and manual (an owner marks a notice "Victoria decision" from
  their own queue). Both call the same `escalate_to_victoria`.
- **Brief generation vs emailing are split.** The auto-escalation always
  generates the `.docx` brief (no external dependency), but does not send it
  automatically, since Microsoft Graph credentials are an external setup
  step that may not exist yet. Sending is a separate, explicit "Send
  escalation email" action on Victoria's dashboard view. Once Graph is
  registered, this could be wired to fire automatically on escalation to
  match SPEC.md B3's wording more literally; flagged as a implementation
  compromise, not silently deviating from the spec's intent.
- **Admin tab rule corrections** require a reason and are restricted to
  Victoria and Kanvesh (Phase B gap 11 above); the update route validates
  submitted column names against the target table's real schema
  (`PRAGMA table_info`) before building the `UPDATE`, so only genuine
  columns can ever be written, never arbitrary SQL.
- **The Flask dashboard is local-only**, per SPEC.md B1 ("Local Flask web
  dashboard"): no CSRF protection library, no rate limiting. Don't expose it
  to the internet as built.

## Testing

```
pytest tests\ -v
```

62 tests cover the OCDS parser (against the real sample notice
`066188-2026_ocds.json`), every gate's documented outcomes, dedupe/fuzzy
matching, the Excel export, the regression comparer, the approval workflow
(ownership enforcement, reason requirements, both escalation triggers), the
B2 scope read (against a mocked Anthropic client, no live API calls), the
B3 brief generator (all ten sections, exact notice title, PROVISIONAL
labelling), and the Graph mail whitelist gate (a non-`@bidsavvy.io`
recipient must raise and never reach the network). All pass as of this
build. The full regression test (A5) still needs the real baseline tracker
Excel file (open item 9) to validate against the actual 129-notice manual
sweep, and B2/B3 haven't been exercised against a real Anthropic API key or
a real Microsoft Graph app registration yet, only mocked/unit-tested.
