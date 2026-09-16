# TenderSight Build Specification
Bid Savvy Solutions Ltd | Trifork UK account | Version 1.5 | 19 July 2026, updated 20 July 2026
Prepared for build via Claude Code. UK English throughout. No em dashes anywhere in code, comments, UI or output.

## Version 1.5 build log (changes since v1.0)
The phase sections below are the original v1.0 brief, left intact as the reference spec. This log
records where the actual build has since deviated or moved on, and why, so it doesn't drift silently.

- **Phase A is complete and Phase B is substantially built** (dashboard, approvals, AI scope reads,
  escalation briefs, Graph mail whitelist), ahead of the "propose Phase B after Phase A is proven"
  sequencing in Build instructions below. The regression test against the manual 129-notice baseline
  (A5) has not yet been run against a real baseline file.
- **Confirmed Phase 1 to Phase 2 routing policy (2026-07-20), deviating from B1 and B3's literal
  wording**: PASS, FLAG and MAYBE headline outcomes all skip the owner's Phase 1 approval queue
  entirely and route straight to the automated Phase 2 AI scope read. Only a FAIL lands in the
  owner's queue, for a manual double-check. Victoria escalation (B3) is correspondingly
  owner-mediated, not automatic on a gate FLAG: the owner sees the original gate flag alongside the
  Phase 2 AI read together and marks the notice "Victoria decision" themselves, rather than it
  escalating the instant Phase 1 triage finishes. This was an explicit, deliberated call with Mark,
  not an oversight, made after tracing a real bug where the two behaviours had drifted apart in the
  codebase (routing said one thing, the approval workflow assumed the other).
- **Gate 1 sector classification fix (2026-07-20)**: bare industry vocabulary in
  `config_sector_keywords` (energy, electricity, bank, payments, rail, railway, highways, airline,
  airlines, airways) used to match unconditionally, flooding the pipeline with non-IT noise (steel
  fittings, academy-trust electricity supply contracts, apprenticeship EPA payments, road
  resurfacing). These now require a real product/capability coupling term (the same pattern Gate 2
  already used), applied to Fintech, Aviation, Rail and Transport and Energy. Real effect on the live
  database: Energy 57 -> 13 notices, Rail and Transport 117 -> 31, Fintech 20 -> 7. A generic keyword
  with no coupling evidence FLAGs rather than FAILs (never fail if unsure), so an ambiguous case still
  reaches a human rather than being silently dropped. NHS and Central/Local Government keywords were
  deliberately left unchanged pending the open question below.
- **Gate 1 uncoupled-keyword outcome changed to FAIL (2026-07-21)**: superseding the "never fail if
  unsure" FLAG-to-Victoria call above, a bare industry keyword with no product/capability coupling
  term is now treated as out of sector and FAILs outright, same as a notice with no sector mention at
  all. Rationale: with no sector matched, there is no owner to route it to, so it was never actually
  landing in an owner's queue for review anyway (per the Phase 1 routing policy above, only a FAIL
  with an owner gets a manual double-check) -- it was only ever visible to Victoria's all-notices
  view, unreviewed by design. This does not affect the "contested" case (buyer/text matches more than
  one configured sector): that is a genuine in-scope match, still FLAGs to Victoria to pick the right
  owner.
- **Victoria escalation now only reachable after Phase 2 (2026-07-21)**: an owner could previously
  mark a Phase 1 (TO_REVIEW) notice for Victoria's decision directly, skipping Phase 2 entirely --
  too many raw, un-scoped leads were reaching her this way. The "Victoria" escalate action (button
  and route) is now only available once a notice reaches AWAITING_PHASE2_APPROVAL; the state machine
  (`models/notice.py`) no longer allows a TO_REVIEW -> ESCALATED_TO_VICTORIA transition, and
  `mark_victoria_decision` (`workflow/approvals.py`) explicitly rejects any other status. A Phase 1
  FAIL/FLAG must still go through the automated Phase 2 scope read (as already established by the
  2026-07-20 routing policy above) before an owner can escalate it.
- **Dashboard consolidation (2026-07-20)**: an unwired, schema-mismatched duplicate dashboard
  (`dashboard-v2.html` plus a parallel JSON API querying columns that don't exist in the real schema)
  had been built alongside the real one and documented in since-removed root docs as "the new main
  dashboard, fully wired" -- it was not reachable from any route and would have 500'd on first use.
  Retired. A proper Overview landing page (stat tiles, a "needs your attention" list, recent activity)
  was added at `/`, and the approval queue moved to `/queue` (same endpoint name, nothing else changed).
- **B2 (AI scope reads) is not yet live in production**: no `ANTHROPIC_API_KEY` is configured, so
  notices reach `PHASE2_SCOPED` and wait there rather than advancing to `AWAITING_PHASE2_APPROVAL`.
- **Manual Phase 2 advance without an AI scope read (2026-07-21)**: with no `ANTHROPIC_API_KEY`
  configured, 2,123 notices had accumulated in `PHASE2_SCOPED`, permanently stuck waiting for a
  scope read that never runs. The extracted notice fields give an owner enough to review at Phase 2,
  so the backend retains manual single and bulk advance routes. The queue no longer exposes the bulk
  "Proceed without AI read" button; the normal visible action remains "Process Phase 2".

## What this is
An automated UK procurement workflow engine with human approval gates. It sweeps official tender sources daily, triages every notice against the Bid Savvy gate model, routes results through owner approvals, escalates Victoria-decision items to her with an auto-drafted brief, and on approval downloads public documents, pushes deadlines to Outlook calendars, and chases follow-ups internally. Humans decide; the machine prepares.

## Non-negotiable rules (enforce in code, not convention)
1. Never invent figures, contacts, deadlines or facts. Any field not present in source data is stored and displayed as UNVERIFIED.
2. Outbound email and messages are whitelisted to @bidsavvy.io addresses only. The tool must never contact a buyer, a portal, or Trifork. Hard-coded check before every send.
3. No value threshold ever. Value is recorded, never used to fail or downgrade.
4. SC or DV clearance requirement = FLAG, never FAIL, never rated HIGH or MED.
5. All AI-generated assessments are labelled PROVISIONAL, FOR VALIDATION. Bid and no-bid decisions belong to Victoria Milan, Bid Director.
6. Scrape sources: public pages only, respect robots.txt, never attempt authentication.
7. Every status change, approval, rejection and settings change is logged with user, timestamp and reason.
8. Secrets (API keys, Graph credentials) live in a .env file, never in code or the repository.

---

## PHASE A: Core engine (build and prove this first)

### A1. State machine and database
SQLite database. Every notice moves through statuses:
NEW → PHASE1_TRIAGED → AWAITING_PHASE1_APPROVAL → PHASE2_SCOPED → AWAITING_PHASE2_APPROVAL → ESCALATED_TO_VICTORIA → APPROVED → DOCS_DOWNLOADED → CALENDARED → ACTIVE, plus REJECTED, PARKED and MONITOR.
No status can be skipped. Store the raw notice JSON with every row as an evidence snapshot so any assessment can be traced to what the notice said on the day. Record the Procurement Act 2023 stage (UK1 to UK5) on every notice. Daily automated database backup.

### A2. Sweep
Pull all new notices from the Find a Tender OCDS API (find-tender.service.gov.uk) and the Contracts Finder API, paginating fully, default lookback 7 days (configurable). A sample OCDS notice is in this folder (066188-2026_ocds.json); build the parser against its real structure.
Dedupe and cross-check: before creating a row, check the notice reference AND fuzzy-match title plus buyer against all existing rows, so the same opportunity surfacing under a different listing updates the existing row rather than creating a duplicate.
Expiry radar: also sweep award notices in scope sectors and log contract end dates. Surface contracts expiring within 18 months as future re-procurement leads with a review date.

### A3. Phase 1 triage gates (rule-based, automated)
Run in order, record every gate result and the first failure.
- Gate 1, buyer sector and owner: NHS and healthcare buyers = Hammad. Central and Local Government, Energy = Kanvesh. Fintech, Aviation (airlines only; airports, ATC and defence aviation fail), Rail and Transport = Mark. Ambiguous or contested = FLAG to Victoria.
- Gate 2, type of work: bespoke build, integration, data, platform = PASS. Hardware, packaged product resale, managed service, resale = FAIL.
- Gate 3, framework status: call-off from a framework Trifork is not on = FAIL (reason: Framework required, Trifork not yet on framework). Framework establishment bid = PASS. Direct open procurement = PASS. Unclear = FLAG, never guess. For UK1/UK2 PME-stage notices with no framework stated, record route as "Route not yet decided" with outcome Maybe, not a false flag.
- Gate 4, window: closed and awarded = FAIL (log re-tender date if visible). Closed PME but tender stage still ahead = MONITOR, not fail. Open = PASS.
- Gate 5, CPV codes: PASS list 72200000, 72212000, 72250000, 72263000, 72310000, 72400000, 72500000. Adjacent 72xxx and 73xxx = INFERRED FIT, proceed and flag. 48xxx = FLAG to Victoria (open question: Trifork as product vendor for Corax and Tiris), never auto-fail, never clean pass. FAIL list: 33xxx, 32xxx, 45xxx, 66xxx, 50xxx, 73430000, 80420000 when bundled with secure testing, 48190000 when bundled with testing. Unlisted codes = FLAG, do not rate.
- Filter 3, scale and incumbents: over £500,000,000 where the dominant supplier pool is global SI primes (IBM, Capgemini, Accenture, Atos, Capita, Leidos, BAE AI, CGI, Cognizant and peers) = FAIL.

### A4. Excel tracker output
On-demand export, multi-sheet, matching the existing team structure: Phase 1 - Flags, To review, Handoffs (split by owner), Closed or awarded, Out of scope - no owner, Phase 2 - Pipeline, Legend and method.
Columns: Ref, Date spotted, Opportunity title (exact from notice, no paraphrasing), Buyer, Owner, Source, Indicative value, CPV codes, UK stage, Gate results, Outcome, Fail reason, Next action, Next action date, Flags.

### A5. Regression test
A test mode that runs the engine over a specified historical date range and outputs a comparison report against a provided baseline tracker Excel file, row by row, listing every disagreement between machine outcome and human outcome. This validates the engine against the manual 129-notice sweep before it is trusted.

---

## PHASE B: Workflow and judgement (build after Phase A is proven)

### B1. Approval dashboard
Local Flask web dashboard with four named user accounts (Mark, Kanvesh, Hammad, Victoria), individual logins, no shared accounts. Queues per status. Phase 1 results wait for the sector owner's Approve or Reject; every rejection requires a stored reason. Nothing proceeds without the click.

### B2. AI scope reads (Phase 2)
On Phase 1 approval, call the Claude API (Anthropic SDK, key in .env) with the full notice text and a Trifork capability profile from config, producing a structured provisional assessment: capability fit, competitor position, right to win, overall, each with one-line reasoning, plus open questions. Output labelled PROVISIONAL, FOR VALIDATION. Results queue for owner approval, including proposed fails with reasons.

### B3. Victoria escalation with auto-brief
Any FLAG at any gate, plus any item an owner marks "Victoria decision", auto-generates a draft Word capture brief (ten sections: opportunity summary, buyer, value, route to market, gate outcomes, provisional ratings with reasoning, competitor picture, risks, open questions, recommended next action) and emails it to victoria.milan@bidsavvy.io via Microsoft Graph, subject "TRIAGE ESCALATION: [exact notice title]", clearly labelled as an auto-generated provisional draft for her validation. Her decision is entered on the dashboard and unlocks, parks or rejects the item.

### B4. Learning loop
Every Victoria ruling and rule correction is entered in the admin tab and stored with date, source and reason, version-log style. Gate logic reads live rules from the database so the engine tightens with every decision and never silently drifts.

---

## PHASE C: Automation around the edges (build last)

### C1. Document harvest
When an item reaches APPROVED, download every publicly attached notice document into a folder named by notice reference. Documents behind portal logins are logged as a manual task assigned to the owner, never attempted by the tool.

### C2. Calendar
Push every deadline (PME close, clarification deadline, submission date, Victoria decision date) to the owner's Outlook calendar via Microsoft Graph, reminders at 14, 7 and 2 days. Fallback to .ics file generation when Graph is not configured.

### C3. Follow-up engine (internal only)
Daily checks: items in an approval queue more than 3 working days, deadlines within 14 days, escalations with no Victoria decision by the decision date. Nudges by Graph email and a post to the Teams channel #trifork-pipeline. Whitelist rule from the top of this spec applies to every send.

### C4. Microsoft Lists sync (optional toggle)
Push approved rows to the Microsoft Lists pipeline tracker via Graph so the tracker of record stays current without double entry. Field mapping in config.

### C5. Admin tab
Dashboard section for managing, with full change logging:
- Tender sources in three tiers: API toggles (FTS, Contracts Finder); RSS or alert feed URLs; best-effort public scrape URLs. Per-source health indicator and last-successful-run timestamp. Scraped fields marked UNVERIFIED at source.
- CPV lists (pass, inferred, flag, fail), keyword include and exclude lists.
- Buyer-to-owner mapping, sector boundaries.
- Email whitelist, nudge thresholds, Lists field mapping.

### C6. Friday EOW report
Auto-generated summary per owner: new opportunities added, opportunities qualified out with reasons, live pursuits by stage, flags awaiting Victoria, next actions due. Emailed to the team Friday afternoon.

---

## Build instructions for Claude Code
1. Propose a plan for Phase A only. Wait for approval before writing code.
2. Python 3.11+, clean modules, type hints, a single config layer backed by the database, README with setup for API access, Microsoft Graph app registration (mail, calendar, Teams, Lists permissions) and Windows Task Scheduler for the daily sweep.
3. After Phase A passes the regression test, propose Phase B, then Phase C.

## Open questions for Victoria (do not resolve in code; keep visible in the README)
1. Canonical gate model across all three desks (five-gate assumed here).
2. Confirmed buyer lists for Fintech, Aviation and Energy (currently draft).
3. 48xxx CPV ruling where Trifork is the product vendor (Corax, Tiris).
4. Whether this tool's output feeds or replaces the Microsoft Lists tracker as record.
5. Approval that auto-emailed escalation briefs to her are wanted at this cadence.
6. Central and Local Government / NHS and Healthcare sector activation (added 2026-07-20):
   `references/Trifork_Scouting_Project_Instructions.md` confirms only four sectors in scope
   (Fintech, Aviation, Rail and Transport, Energy) and says explicitly not to scout secondary
   sectors until confirmed. The live build still actively scouts both anyway (matching this
   document's original six-sector Gate 1 list), and they currently make up 52% of total swept
   volume. Left as is pending Kanvesh's confirmation one way or the other; not yet resolved.
7. Victoria escalation timing (added 2026-07-20): B3 above says "any FLAG at any gate" escalates
   automatically. The live build instead routes FLAG/MAYBE through the automated Phase 2 scope read
   first and leaves escalation to the owner's judgement at that point (see the v1.5 build log
   above). Confirmed with Mark as the intended model for now; flagging here since it is a literal
   deviation from this document's own B3 wording.

Smarter Bids. Real Results.  |  © 2026 Bid Savvy Solutions Ltd
