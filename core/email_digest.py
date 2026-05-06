"""
email_digest.py  —  MAPlanning weekly email digest.

Two ways to use:
  1. Called automatically by the scraper at the end of each run
     (via send_digest() — passes new leads directly, no sheet re-read needed)
  2. Run standalone to send the full sheet history
     (python email_digest.py — calls send(), reads sheet directly)

Env vars required (set as GitHub Secrets):
  GCP_SERVICE_ACCOUNT_JSON  — full service account JSON string
  GMAIL_FROM                — sender Gmail address  e.g. hello@planningscout.co.uk
  GMAIL_APP_PASSWORD        — Gmail App Password (16 chars, no spaces)
  GMAIL_TO                  — comma-separated recipients
                              e.g. "you@gmail.com" for test
                              e.g. "inger.balaj@gmail.com" for Mark
                              e.g. "you@gmail.com,contact@maplanning.co.uk" for both
"""

import os, json, sys, smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import gspread
from google.oauth2.service_account import Credentials

# ════════════════════════════════════════════════════════════
# CONFIG  ← the only section you ever need to edit
# ════════════════════════════════════════════════════════════

SHEET_ID   = "172bpv-b2_nK5ENE1XPk5rWeokvnr1sjHvLBfVzHWh6c"
SHEET_NAME = "Leads"

# ── DAYS_BACK ────────────────────────────────────────────────
# Controls how many days back to look when reading from the sheet.
#
# Normal weekly digest → set to 8
# Send full history (e.g. first send to Mark) → set to 999
#
DAYS_BACK  = 999    # ← CHANGE THIS to 999 to send everything in the sheet

# ── EMAIL RECIPIENT ──────────────────────────────────────────
# This is the FALLBACK if GMAIL_TO secret is not set in GitHub.
# It is ALWAYS overridden by the GMAIL_TO GitHub Secret when running
# in GitHub Actions. So to change who gets the email:
#
#   → Go to: GitHub repo → Settings → Secrets and variables
#            → Actions → find GMAIL_TO → edit it
#
# Change GMAIL_TO secret to:
#   "your@email.com"                    ← just you (test)
#   "you@email.com,contact@maplanning.co.uk"  ← both
#
FALLBACK_TO = "inger.balaj@gmail.com"   # ← only used if GMAIL_TO secret is missing

# ════════════════════════════════════════════════════════════
# COLUMN MAP — matches the Google Sheet column order exactly
# ════════════════════════════════════════════════════════════
COL = dict(
    council=0, ref=1, addr=2, desc=3, app_type=4,
    applicant=5, agent=6, date_rec=7, date_dec=8,
    decision=9, triggers=10, score=11, keyword=12,
    portal=13, dec_doc=14, date_found=15, comments=16,
    est_value=17, developer=18, architect=19,
    impact_prob=20, ch_number=21, reg_addr=22, contact_link=23
)

def cell(row, key):
    idx = COL.get(key, -1)
    if idx < 0 or idx >= len(row): return ""
    return str(row[idx]).strip()

# ════════════════════════════════════════════════════════════
# FETCH LEADS FROM SHEET  (used by standalone send() only)
# ════════════════════════════════════════════════════════════
def load_leads_from_sheet():
    """
    Read leads from the Google Sheet.
    Returns (new_leads_list, total_row_count).
    'new' means added within the last DAYS_BACK days.
    Set DAYS_BACK = 999 to return everything.
    """
    sa_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        print("❌ GCP_SERVICE_ACCOUNT_JSON not set"); sys.exit(1)
    info  = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ])
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
    rows = ws.get_all_values()[1:]   # skip header row

    cutoff = datetime.now() - timedelta(days=DAYS_BACK)
    new_leads   = []
    total_leads = len(rows)

    for row in rows:
        # 1. Get the date string from 'Date Decided' (column 8)
        # We use this because 'Date Found' might be empty in old leads
        df_str = cell(row, "date_dec")
        
        if not df_str:
            # Fallback: if no decision date, use 'now' so it shows up for testing
            df = datetime.now()
        else:
            try:
                # Clean "Wed 18 Feb 2026" -> "18 Feb 2026"
                parts = df_str.split()
                if len(parts) > 3:
                    clean_date = " ".join(parts[1:4]) # Takes '18', 'Feb', '2026'
                    df = datetime.strptime(clean_date, "%d %b %Y")
                else:
                    df = datetime.now()
            except Exception:
                df = datetime.now()

        # 2. Check if the lead is 'new' enough (999 days covers everything)
        if df >= cutoff:
            try:    sc   = int(cell(row, "score"))
            except: sc   = 0
            
            # Clean the probability (e.g., "75%" -> 75)
            prob_str = str(cell(row, "impact_prob")).replace("%", "").strip()
            try:    prob = int(prob_str) if prob_str else 0
            except: prob = 0

            new_leads.append({
                "council":      cell(row, "council"),
                "ref":          cell(row, "ref"),
                "addr":         cell(row, "addr"),
                "desc":         cell(row, "desc"),
                "applicant":    cell(row, "applicant"),
                "agent":        cell(row, "agent"),
                "date_dec":     cell(row, "date_dec"),
                "triggers":     cell(row, "triggers"),
                "score":        sc,
                "portal":       cell(row, "portal"),
                "est_value":    cell(row, "est_value"),
                "developer":    cell(row, "developer"),
                "architect":    cell(row, "architect"),
                "impact_prob":  prob,
                "contact_link": cell(row, "contact_link"),
                "ch_number":    cell(row, "ch_number"),
            })

    new_leads.sort(key=lambda x: x["score"], reverse=True)
    print(f"✅ Sheet: {total_leads} total rows, {len(new_leads)} within last {DAYS_BACK} days")
    return new_leads, total_leads


# ════════════════════════════════════════════════════════════
# HTML BUILDERS
# ════════════════════════════════════════════════════════════
def _sc_color(s):
    return "#16a34a" if s >= 75 else "#d97706" if s >= 55 else "#dc2626"

def _p_color(p):
    return "#dc2626" if p >= 75 else "#d97706" if p >= 50 else "#16a34a"

def _plabel(s):
    return "A — High Priority" if s >= 75 else "B — Medium" if s >= 55 else "C — Low"

def _card(lead):
    sc   = lead["score"]
    prob = lead["impact_prob"]
    col  = _sc_color(sc)
    pc   = _p_color(prob)
    dev  = lead["developer"] or lead["applicant"] or "—"
    arch = lead["architect"] or lead["agent"] or "—"
    ch   = lead["contact_link"]
    val  = lead["est_value"]

    chips = "".join(
        '<span style="background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;'
        'border-radius:20px;padding:1px 8px;font-size:11px;margin:2px 2px 0 0;'
        f'display:inline-block;">{t.strip()}</span>'
        for t in lead["triggers"].split(",") if t.strip()
    )
    dev_html = (
        f'<a href="{ch}" style="color:#1e40af;text-decoration:none;">{dev}</a>'
        if ch else dev
    )
    val_badge = (
        '<span style="background:#f0fdf4;color:#15803d;border:1px solid #bbf7d0;'
        'border-radius:20px;padding:2px 10px;font-size:11px;font-weight:600;margin-left:6px;">'
        f'&#128176; {val}</span>'
    ) if val else ""

    btns = ""
    if lead["portal"]:
        btns += (
            f'<a href="{lead["portal"]}" style="display:inline-block;margin-right:8px;'
            f'padding:6px 14px;background:#1e40af;color:#fff;border-radius:6px;'
            f'font-size:12px;text-decoration:none;">&#128196; View Application</a>'
        )
    if ch:
        btns += (
            f'<a href="{ch}" style="display:inline-block;padding:6px 14px;'
            f'background:#374151;color:#fff;border-radius:6px;font-size:12px;'
            f'text-decoration:none;">&#127968; Companies House</a>'
        )

    return (
        f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;'
        f'margin-bottom:16px;border-left:4px solid {col};">'
        f'<div style="padding:16px 20px 14px;">'
        f'<div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:8px;">'
        f'<span style="background:#dbeafe;color:#1e40af;border-radius:20px;font-size:11px;'
        f'font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:2px 9px;">'
        f'{lead["council"]}</span>'
        f'<span style="font-family:monospace;font-size:11px;color:#6b7280;">{lead["ref"]}</span>'
        f'<span style="background:{col}20;color:{col};border:1px solid {col}40;'
        f'border-radius:20px;padding:2px 9px;font-size:11px;font-weight:600;">{_plabel(sc)}</span>'
        f'<span style="font-family:monospace;font-size:14px;font-weight:700;color:{col};'
        f'margin-left:auto;">{sc}/100</span>{val_badge}</div>'
        f'<div style="font-size:14px;color:#111827;margin-bottom:5px;line-height:1.5;">'
        f'{lead["desc"][:220]}</div>'
        f'<div style="font-size:12px;color:#6b7280;margin-bottom:8px;">&#128205; '
        f'{lead["addr"][:90]}&nbsp;&#183;&nbsp;&#128197; {lead["date_dec"]}</div>'
        f'<div style="margin-bottom:10px;">{chips}</div>'
        f'<div style="margin-bottom:12px;">'
        f'<div style="font-size:11px;color:#6b7280;margin-bottom:4px;">Impact study probability: '
        f'<strong style="color:{pc};">{prob}%</strong></div>'
        f'<div style="background:#e5e7eb;border-radius:3px;height:5px;">'
        f'<div style="background:{pc};height:5px;border-radius:3px;'
        f'width:{min(prob,100)}%;"></div></div></div>'
        f'<div style="background:#f9fafb;border-radius:6px;padding:10px 12px;'
        f'margin-bottom:12px;font-size:12px;color:#374151;">'
        f'<div style="margin-bottom:4px;">&#127970; <strong>Developer:</strong> {dev_html}</div>'
        f'<div>&#128208; <strong>Architect / Agent:</strong> {arch}</div></div>'
        f'<div>{btns}</div>'
        f'</div></div>'
    )


def build_html(leads, total, run_stats=None):
    """
    Build the full HTML email.
    run_stats: optional dict with keys new_this_run, councils_tried, failed, duration_min
    """
    n     = len(leads)
    high  = sum(1 for l in leads if l["score"] >= 75)
    avg_s = int(sum(l["score"] for l in leads) / n) if n else 0
    avg_p = int(sum(l["impact_prob"] for l in leads) / n) if n else 0
    run_dt = datetime.now().strftime("%A %d %B %Y, %H:%M UTC")
    cutoff = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%d %b %Y")

    cards   = "".join(_card(l) for l in leads)
    no_leads = (
        '<div style="text-align:center;padding:40px;color:#6b7280;font-size:14px;">'
        'No new qualified leads found this period.</div>'
    ) if not leads else ""

    stats_cells = "".join(
        '<td style="width:25%;text-align:center;">'
        '<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 8px;">'
        f'<div style="font-family:monospace;font-size:1.5rem;font-weight:700;color:{col};">{val}</div>'
        f'<div style="font-size:11px;color:#9ca3af;text-transform:uppercase;'
        f'letter-spacing:.07em;margin-top:3px;">{lbl}</div>'
        '</div></td>'
        for val, lbl, col in [
            (n,           "Leads in digest", "#1e40af"),
            (high,        "A Priority",      "#16a34a"),
            (f"{avg_s}",  "Avg Score",       "#d97706"),
            (f"{avg_p}%", "Impact Prob",     "#dc2626"),
        ]
    )

    # Optional run-stats banner (shown when called from scraper)
    run_banner = ""
    if run_stats:
        new   = run_stats.get("new_this_run", 0)
        tried = run_stats.get("councils_tried", 0)
        dur   = run_stats.get("duration_min", 0)
        fail  = run_stats.get("failed", [])
        run_banner = (
            '<div style="background:#1e293b;border-radius:8px;padding:12px 16px;'
            'margin-bottom:12px;font-size:12px;color:#94a3b8;">'
            f'&#9881;&#65039; This run: <strong style="color:#fff;">{new} new leads found</strong> '
            f'across {tried} councils in {dur:.0f} min'
            + (f' &nbsp;&#183;&nbsp; {len(fail)} councils failed' if fail else '')
            + '</div>'
        )

    heading = "All Qualified Leads" if DAYS_BACK >= 90 else "New Qualified Leads This Week"

    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"/></head>'
        '<body style="margin:0;padding:0;background:#f3f4f6;'
        'font-family:Helvetica Neue,Arial,sans-serif;">'
        '<div style="max-width:680px;margin:0 auto;padding:24px 16px;">'
        '<div style="background:#0f172a;border-radius:12px;padding:28px 32px;margin-bottom:16px;">'
        '<div style="font-size:22px;font-weight:700;color:#fff;margin-bottom:4px;">'
        '&#127959; MAPlanning</div>'
        '<div style="font-size:13px;color:#94a3b8;margin-bottom:12px;">'
        'Retail Lead Intelligence &middot; Weekly Digest</div>'
        f'<div style="font-size:12px;color:#64748b;">&#128197; Leads added since {cutoff}'
        f' &nbsp;&middot;&nbsp; {run_dt}</div>'
        f'<div style="font-size:12px;color:#475569;margin-top:6px;">'
        f'Total in sheet: <strong style="color:#94a3b8;">{total}</strong></div>'
        '</div>'
        f'{run_banner}'
        f'<table style="width:100%;border-collapse:separate;border-spacing:8px;'
        f'margin-bottom:16px;"><tr>{stats_cells}</tr></table>'
        f'<div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:12px;'
        f'letter-spacing:.06em;text-transform:uppercase;">'
        f'{"No New Leads This Period" if not leads else heading}</div>'
        f'{cards}{no_leads}'
        '<div style="text-align:center;padding:20px 0 8px;font-size:11px;color:#9ca3af;">'
        f'MAPlanning Retail Intelligence &middot; Automated digest &middot; '
        f'{datetime.now().year}'
        '</div></div></body></html>'
    )


# ════════════════════════════════════════════════════════════
# SEND HELPERS
# ════════════════════════════════════════════════════════════
def _get_smtp_config():
    """Read email credentials from environment variables."""
    gmail_user = os.environ.get("GMAIL_FROM", "").strip()
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to_raw     = os.environ.get("GMAIL_TO", FALLBACK_TO)
    recipients = [r.strip() for r in to_raw.split(",") if r.strip()]
    if not gmail_user or not gmail_pass:
        print("❌ GMAIL_FROM or GMAIL_APP_PASSWORD not set"); sys.exit(1)
    return gmail_user, gmail_pass, recipients


def _send_raw(gmail_user, gmail_pass, recipients, subject, html, log_fn=None):
    """Low-level: compose and send the email via Gmail SMTP SSL."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"MAPlanning <{gmail_user}>"
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_pass)
        smtp.sendmail(gmail_user, recipients, msg.as_string())
    out = f"✅ Email sent → {recipients}  |  Subject: {subject}"
    (log_fn or print)(out)


# ════════════════════════════════════════════════════════════
# send_digest()  — called by the SCRAPER at end of each run
# ════════════════════════════════════════════════════════════
def send_digest(new_leads, summary, failed, date_from, date_to,
                weekly_count=0, weekly_leads=None, run_duration_min=0,
                log_fn=None):
    """
    Called automatically by maplanning.py after scraping completes.

    Parameters
    ----------
    new_leads       : list of lead dicts found in THIS run
    summary         : dict  {council_name: int leads_found}
    failed          : list  [council_name, ...]
    date_from/to    : scrape date range strings
    weekly_count    : total leads added in last 7 days (from sheet)
    weekly_leads    : list of those leads (may be empty if not fetched)
    run_duration_min: float, how long the scrape took
    log_fn          : callable for logging (defaults to print)
    """
    log = log_fn or print

    gmail_user, gmail_pass, recipients = _get_smtp_config()

    # Use weekly_leads from sheet if available; fall back to new_leads from this run
    leads_to_show = weekly_leads if weekly_leads else new_leads
    total_in_sheet = weekly_count  # approximate — exact count from sheet

    run_stats = {
        "new_this_run":    len(new_leads),
        "councils_tried":  len(summary),
        "failed":          failed,
        "duration_min":    run_duration_min,
    }

    n       = len(leads_to_show)
    subject = (
        f"MAPlanning · {n} lead{'s' if n != 1 else ''} this week (run complete)"
        if n else "MAPlanning · No new leads this week (run complete)"
    )

    html = build_html(leads_to_show, total_in_sheet, run_stats=run_stats)
    _send_raw(gmail_user, gmail_pass, recipients, subject, html, log_fn=log)


# ════════════════════════════════════════════════════════════
# send()  — called when running email_digest.py STANDALONE
#           e.g. python email_digest.py
# ════════════════════════════════════════════════════════════
def send():
    """
    Standalone mode: reads directly from Google Sheet and sends.
    Useful for:
      - Testing the email format
      - Sending the full history to a new client
      - Running a manual digest outside the scraper schedule
    """
    leads, total = load_leads_from_sheet()
    gmail_user, gmail_pass, recipients = _get_smtp_config()

    n = len(leads)
    subject = (
        f"MAPlanning · {n} lead{'s' if n != 1 else ''} (last {DAYS_BACK} days)"
        if n else f"MAPlanning · No leads found (last {DAYS_BACK} days)"
    )
    html = build_html(leads, total)
    _send_raw(gmail_user, gmail_pass, recipients, subject, html)


if __name__ == "__main__":
    send()
