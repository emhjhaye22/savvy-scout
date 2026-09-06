"""Settings: read-only surfacing of controls that already exist in the
backend but had no UI before this -- the outbound notice-email whitelist
domain and the tracker export's data dictionary. Both are sourced live from
the actual code (mail.py's ALLOWED_DOMAIN, trifork_pipeline.py's HEADERS),
not a separately maintained document, so this view can never drift out of
date with what the app actually does.

The whitelist is deliberately NOT editable here. SPEC.md non-negotiable 2
makes @bidsavvy.io the one allowed outbound domain, enforced by a hard,
non-bypassable check in graph/mail.py's assert_whitelisted(). Turning that
into a "manage senders" UI would need new backend infrastructure (a config
table, an audit trail) for a rule that's supposed to never change from the
UI -- the same "trim scope back" guidance that applies to the optional
API-key/MCP panel this screen also leaves out."""

from flask import Blueprint, render_template
from flask_login import login_required

from savvy_scout.dashboard.auth import get_db
from savvy_scout.export.trifork_pipeline import HEADERS as TRACKER_COLUMNS
from savvy_scout.graph.mail import ALLOWED_DOMAIN
from savvy_scout.sweep.runner import get_recent_sweep_runs

settings_bp = Blueprint("settings", __name__)

# One line per HEADERS entry, same order, describing where each column's
# value actually comes from (savvy_scout/export/trifork_pipeline.py's
# _row_values) -- kept here rather than in trifork_pipeline.py itself so a
# read-only settings page doesn't need to import row-building internals.
_COLUMN_DESCRIPTIONS = (
    "The notice's own reference number, as published by the source portal.",
    "The date the notice was first published.",
    "The notice title as published.",
    "The contracting authority or buyer named on the notice.",
    "Central government / sub-central / private sector, mapped from the buyer's organisation type.",
    "The Trifork sector this notice was triaged under (one of the six configured sectors).",
    "Which portal the notice was pulled from (e.g. Find a Tender, Contracts Finder).",
    "The UK procurement stage (UK1 Pipeline through UK5 Award), described in full.",
    "The notice's stated contract value, with currency symbol, or \"Not stated\" if none was published.",
    "The CPV codes recorded against the notice, semicolon-separated.",
    "The submission or market-engagement deadline stated on the notice.",
    "Which tracker sheet the row landed on: PASS, FLAG, or FAIL, per the owner's Phase 2 decision.",
    "The Phase 2 AI capability-fit rating plus its one-line reasoning.",
    "Framework route status (already on a framework, route not yet decided, or unconfirmed).",
    "Any of the three named risk filters (capability/market mismatch, UK security clearance, scale) that this notice tripped.",
    "The narrative reason for the row's outcome -- grounded in this specific notice, not a generic label.",
    "The recommended next step for this opportunity.",
    "The date the next action above falls due, where applicable.",
    "The open question this row raises for Victoria to decide, if any.",
    "A relative link to the generated Internal Addendum document, if one has been produced.",
    "A relative link to the generated Capture Brief document, if one has been produced.",
)


@settings_bp.route("/settings")
@login_required
def index():
    conn = get_db()
    data_dictionary = list(zip(TRACKER_COLUMNS, _COLUMN_DESCRIPTIONS))
    # Moved here from the Overview 2026-09-06, on request -- this is
    # operational/diagnostic detail (per-source sweep success/failure),
    # not something that belongs on a daily-glance business dashboard.
    sweep_history = get_recent_sweep_runs(conn)
    return render_template(
        "settings.html",
        allowed_domain=ALLOWED_DOMAIN,
        data_dictionary=data_dictionary,
        sweep_history=sweep_history,
    )
