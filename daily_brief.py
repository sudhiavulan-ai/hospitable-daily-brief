#!/usr/bin/env python3
"""
Hospitable Daily Check-in Brief
================================
Pulls today's check-ins from Hospitable, audits whether pet fees and pool
heating fees were properly collected on each one, and emails a summary
to the configured recipient via Resend.

Designed to run as a GitHub Actions cron job. See README for setup.

Environment variables required:
    HOSPITABLE_TOKEN  - Hospitable Personal Access Token
    RESEND_API_KEY    - Resend API key (re_...)
    EMAIL_TO          - Recipient email
    EMAIL_FROM        - Sender email (default: onboarding@resend.dev)
    SEND_WINDOW_HOUR  - Local hour at which to send (default: 7)
    SEND_TIMEZONE     - IANA timezone (default: America/Chicago)
    PET_FEE_PER_PET   - Expected pet fee in dollars per pet (default: 150)
    POOL_HEATING_PER_NIGHT - Expected pool heating per night, dollars (default: 50)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from html import escape as html_escape
from typing import Any

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:
    print("Python 3.9+ required (need zoneinfo)", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOSPITABLE_TOKEN = os.environ.get("HOSPITABLE_TOKEN", "").strip()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
EMAIL_TO = os.environ.get("EMAIL_TO", "avulastays@gmail.com").strip()
EMAIL_FROM = os.environ.get("EMAIL_FROM", "onboarding@resend.dev").strip()
SEND_HOUR = int(os.environ.get("SEND_WINDOW_HOUR", "7"))
SEND_TZ = os.environ.get("SEND_TIMEZONE", "America/Chicago")
PET_FEE_PER_PET = float(os.environ.get("PET_FEE_PER_PET", "150"))  # USD per pet/stay
POOL_HEATING_PER_NIGHT = float(os.environ.get("POOL_HEATING_PER_NIGHT", "50"))  # USD per night

HOSPITABLE_API = "https://public.api.hospitable.com/v2"

if not HOSPITABLE_TOKEN:
    print("FATAL: HOSPITABLE_TOKEN not set", file=sys.stderr)
    sys.exit(1)
if not RESEND_API_KEY:
    print("FATAL: RESEND_API_KEY not set", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# DST-safe send window — only proceed if local time is within ±30 min of SEND_HOUR
# (cron is scheduled twice daily so one fire always lands in window regardless of DST)
# ---------------------------------------------------------------------------

local_now = datetime.now(ZoneInfo(SEND_TZ))
window_open = local_now.hour == SEND_HOUR and local_now.minute < 30
window_force = os.environ.get("FORCE_SEND") == "1"

if not (window_open or window_force):
    print(
        f"Outside send window — current {SEND_TZ} time is "
        f"{local_now:%H:%M}, target hour {SEND_HOUR:02d}. Skipping. "
        f"(Set FORCE_SEND=1 to override.)"
    )
    sys.exit(0)

today = local_now.date()
today_iso = today.isoformat()
print(f"Generating brief for {today_iso} ({SEND_TZ})")


# ---------------------------------------------------------------------------
# Hospitable API helpers
# ---------------------------------------------------------------------------

def hospitable_get(path: str, params: dict[str, Any] | None = None) -> dict:
    """Call a Hospitable API endpoint. params lists are encoded as `key[]=v1&key[]=v2`."""
    url = f"{HOSPITABLE_API}{path}"
    if params:
        # urlencode with doseq=True handles list values, but Hospitable wants `key[]=v` form,
        # so transform list-valued params before encoding.
        flat: list[tuple[str, str]] = []
        for k, v in params.items():
            if isinstance(v, list):
                for item in v:
                    flat.append((f"{k}[]", str(item)))
            else:
                flat.append((k, str(v)))
        url = f"{url}?{urllib.parse.urlencode(flat)}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {HOSPITABLE_TOKEN}",
            "Accept": "application/json",
            "User-Agent": "hospitable-daily-brief/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Hospitable API {e.code} on {path}: {body[:300]}") from e


# ---------------------------------------------------------------------------
# 1. Fetch all properties
# ---------------------------------------------------------------------------

print("Fetching properties...")
props_resp = hospitable_get("/properties", {"per_page": 100})
properties = props_resp.get("data", [])
prop_lookup = {p["id"]: p for p in properties}
print(f"  Found {len(properties)} property/properties: {[p['name'] for p in properties]}")


# ---------------------------------------------------------------------------
# 2. Fetch today's check-ins for every property
# ---------------------------------------------------------------------------

print(f"Fetching check-ins for {today_iso}...")
checkins: list[dict] = []
for prop in properties:
    resp = hospitable_get(
        "/reservations",
        {
            "properties": [prop["id"]],
            "start_date": today_iso,
            "end_date": today_iso,
            "date_query": "checkin",
            "include": "financials,guest",
            "per_page": 50,
        },
    )
    for r in resp.get("data", []):
        if r.get("reservation_status", {}).get("current", {}).get("category") == "cancelled":
            continue
        r["_property"] = prop
        checkins.append(r)

# Sort by check-in time
checkins.sort(key=lambda r: r.get("check_in", ""))
print(f"  {len(checkins)} active check-in(s) today")


# ---------------------------------------------------------------------------
# 3. Audit each reservation: pet fee + pool heating
# ---------------------------------------------------------------------------

def find_fee(financials: dict, *needles: str) -> dict | None:
    """Return the first guest-side fee whose label contains any of the needles."""
    fees = (financials or {}).get("guest", {}).get("fees", []) or []
    for f in fees:
        label = (f.get("label") or "").lower()
        if any(n.lower() in label for n in needles):
            return f
    return None


def usd(cents: int | float | None) -> str:
    if cents is None:
        return "—"
    return f"${cents/100:,.2f}"


def fetch_messages(reservation_uuid: str) -> list[dict]:
    """Fetch the message thread for a reservation. Drops automated/AI templates so
    keyword matching only sees real conversation. Returns newest-first."""
    try:
        resp = hospitable_get(f"/reservations/{reservation_uuid}/messages")
    except Exception as e:
        print(f"  ⚠️  Could not fetch messages for {reservation_uuid}: {e}", file=sys.stderr)
        return []
    msgs = resp.get("data", []) or []
    # Strip auto-templates ("automated") and AI auto-replies ("AI") — they
    # mention "pool heating" in every welcome message and would false-positive
    # the topic-detection logic below. Keep manual host messages and guest msgs.
    return [m for m in msgs if (m.get("source") or "").lower() not in ("automated", "ai")]


# Phrases that indicate the topic was discussed
POOL_HEATING_TOPIC_TERMS = [
    "pool heat", "heated pool", "heating the pool", "heat the pool",
    "warm the pool", "pool warmer", "pool heater", "heated the pool",
]
PET_FEE_TOPIC_TERMS = [
    "pet fee", "pet charge", "fee for the pet", "fee for the dog",
    "fee for the cat", "fee for our pet", "fee for our dog", "pet payment",
]
# Phrases that indicate money actually changed hands
PAYMENT_CONFIRMATION_TERMS = [
    "i paid", "we paid", "have paid", "just paid",
    "payment sent", "payment received", "i sent", "we sent", "sent the",
    "transferred", "wired", "deposited", "received your", "received the",
    "got your", "got the payment",
    "venmo", "paypal", "zelle", "cashapp", "cash app", "apple pay",
    "request to pay", "payment request", "i'll pay", "will pay",
]
# Phrases that indicate the guest accepted the offer (commonly leads to a manual payment request)
ACCEPTANCE_TERMS = [
    "yes please", "yes pls", "yes we would", "yes i would", "yes, please",
    "we would like", "i would like", "we'd like", "i'd like",
    "go ahead", "sounds good", "let's do it", "we want", "i want",
    "please heat", "heat it", "heat please", "we accept",
]


def scan_for_topic(messages: list[dict], topic_terms: list[str]) -> dict | None:
    """Look through filtered messages for evidence of payment, acceptance, or any topic mention.
    Returns the strongest signal found, or None.

    Strength ranking (most → least):
        paid_manual    → topic + payment language in same message
        accepted       → topic mention from host AND guest acceptance reply
        host_offered   → host mentioned topic, guest hasn't engaged
        guest_inquired → guest asked about topic, no payment yet
        None           → topic never came up
    """
    if not messages:
        return None

    topic_l = [t.lower() for t in topic_terms]
    payment_l = [t.lower() for t in PAYMENT_CONFIRMATION_TERMS]
    accept_l = [t.lower() for t in ACCEPTANCE_TERMS]

    topic_msgs: list[dict] = []
    paid_msgs: list[dict] = []
    accepted_msgs: list[dict] = []
    guest_topic_msgs: list[dict] = []
    host_topic_msgs: list[dict] = []

    for m in messages:
        body = (m.get("body") or "").lower()
        if not body:
            continue
        sender_type = (m.get("sender_type") or m.get("sender_role") or "").lower()
        if not any(t in body for t in topic_l):
            continue
        topic_msgs.append(m)
        if sender_type == "host":
            host_topic_msgs.append(m)
        else:
            guest_topic_msgs.append(m)
        if any(p in body for p in payment_l):
            paid_msgs.append(m)
        # Acceptance — only count if it's a guest reply
        if sender_type != "host" and any(a in body for a in accept_l):
            accepted_msgs.append(m)

    def snippet(m: dict) -> dict:
        body = (m.get("body") or "").strip().replace("\r\n", " ").replace("\n", " ")
        # Trim leading/trailing whitespace, collapse runs of spaces
        body = " ".join(body.split())
        return {
            "snippet": body[:220] + ("…" if len(body) > 220 else ""),
            "sender": (m.get("sender_type") or m.get("sender_role") or "?").lower(),
            "sender_name": (m.get("sender") or {}).get("first_name") or "",
            "created_at": m.get("created_at", ""),
        }

    if paid_msgs:
        return {"status": "paid_manual", **snippet(paid_msgs[0])}
    if accepted_msgs:
        return {"status": "accepted", **snippet(accepted_msgs[0])}
    if guest_topic_msgs:
        return {"status": "guest_inquired", **snippet(guest_topic_msgs[0])}
    if host_topic_msgs:
        return {"status": "host_offered", **snippet(host_topic_msgs[0])}
    return None


audited: list[dict] = []
for r in checkins:
    guest = r.get("guest") or {}
    party = r.get("guests") or {}
    fin = r.get("financials") or {}

    name = f"{guest.get('first_name','')} {guest.get('last_name','')}".strip() or "Unknown"
    code = r.get("code", "")
    nights = r.get("nights", 0)
    checkin_dt = r.get("check_in", "")
    checkin_time = checkin_dt.split("T")[1][:5] if "T" in checkin_dt else "—"
    departure = r.get("departure_date", "").split("T")[0]

    pet_count = party.get("pet_count", 0) or 0
    adult = party.get("adult_count", 0) or 0
    child = party.get("child_count", 0) or 0
    infant = party.get("infant_count", 0) or 0
    total_party = adult + child + infant

    # --- Fetch messages once per reservation (used by both pet fee and pool heating audits) ---
    msgs = fetch_messages(r["id"]) if r.get("id") else []

    # --- Pet fee audit ---
    pet_fee = find_fee(fin, "pet fee", "pet_fee")
    pet_fee_paid_cents = pet_fee["amount"] if pet_fee else 0
    expected_pet_cents = int(round(pet_count * PET_FEE_PER_PET * 100))
    pet_msg_evidence = None
    if pet_count == 0:
        pet_status = "no pets"
        pet_status_class = "ok-muted"
    elif pet_fee_paid_cents == expected_pet_cents:
        pet_status = f"✅ {usd(pet_fee_paid_cents)} for {pet_count} pet{'s' if pet_count>1 else ''}"
        pet_status_class = "ok"
    elif pet_fee_paid_cents == 0:
        # Financials show no pet fee — but maybe paid manually via message thread
        pet_msg_evidence = scan_for_topic(msgs, PET_FEE_TOPIC_TERMS)
        if pet_msg_evidence and pet_msg_evidence["status"] == "paid_manual":
            pet_status = f"✅ Manual — {pet_count} pet{'s' if pet_count>1 else ''} (see message)"
            pet_status_class = "ok"
        elif pet_msg_evidence and pet_msg_evidence["status"] in ("accepted", "host_offered", "guest_inquired"):
            pet_status = f"⚠️ Discussed but not in financials — verify (see message)"
            pet_status_class = "warn"
        else:
            pet_status = f"❌ NOT COLLECTED — expected {usd(expected_pet_cents)} for {pet_count} pet{'s' if pet_count>1 else ''}"
            pet_status_class = "fail"
    else:
        pet_status = (
            f"⚠️ MISMATCH — paid {usd(pet_fee_paid_cents)}, "
            f"expected {usd(expected_pet_cents)} ({pet_count} × ${PET_FEE_PER_PET:.0f})"
        )
        pet_status_class = "warn"

    # --- Pool heating audit ---
    # Two ways guests pay: (1) line item in booking financials, or (2) manual
    # payment after the host sends a "request to pay" — that path lives only in
    # the message thread, never in the financials block.
    pool_fee = find_fee(fin, "pool heating", "pool heat")
    pool_msg_evidence = None
    if pool_fee:
        paid = pool_fee["amount"]
        expected_full_cents = int(round(nights * POOL_HEATING_PER_NIGHT * 100))
        if paid == expected_full_cents:
            pool_status = f"✅ Booked — {usd(paid)} ({nights} nights × ${POOL_HEATING_PER_NIGHT:.0f})"
            pool_status_class = "ok"
        else:
            implied_nights = paid / 100 / POOL_HEATING_PER_NIGHT if POOL_HEATING_PER_NIGHT else 0
            pool_status = (
                f"⚠️ Partial booking fee — {usd(paid)} (≈ {implied_nights:.0f} of {nights} nights)"
            )
            pool_status_class = "warn"
    else:
        # No booking line item — fall back to scanning the conversation thread
        pool_msg_evidence = scan_for_topic(msgs, POOL_HEATING_TOPIC_TERMS)
        if not pool_msg_evidence:
            pool_status = "Not requested"
            pool_status_class = "ok-muted"
        elif pool_msg_evidence["status"] == "paid_manual":
            pool_status = "✅ Manual payment confirmed (see message)"
            pool_status_class = "ok"
        elif pool_msg_evidence["status"] == "accepted":
            pool_status = "⚠️ Guest accepted — awaiting your payment request"
            pool_status_class = "warn"
        elif pool_msg_evidence["status"] == "host_offered":
            pool_status = "⏳ You offered — no guest reply yet"
            pool_status_class = "warn"
        elif pool_msg_evidence["status"] == "guest_inquired":
            pool_status = "⏳ Guest asked — no payment yet"
            pool_status_class = "warn"
        else:
            pool_status = "Not requested"
            pool_status_class = "ok-muted"

    audited.append({
        "name": name,
        "code": code,
        "platform": (r.get("platform") or "").title(),
        "property_name": r["_property"]["name"],
        "nights": nights,
        "departure": departure,
        "checkin_time": checkin_time,
        "party_total": total_party,
        "adult": adult,
        "child": child,
        "infant": infant,
        "pet_count": pet_count,
        "pet_status": pet_status,
        "pet_status_class": pet_status_class,
        "pet_msg_evidence": pet_msg_evidence,
        "pool_status": pool_status,
        "pool_status_class": pool_status_class,
        "pool_msg_evidence": pool_msg_evidence,
        "phone": (guest.get("phone_numbers") or [None])[0],
        "guest_location": guest.get("location"),
        "language": guest.get("language"),
        "issue_alert": r.get("issue_alert"),
    })


# ---------------------------------------------------------------------------
# 4. Build email HTML
# ---------------------------------------------------------------------------

problem_count = sum(
    1 for a in audited
    if a["pet_status_class"] in ("fail", "warn")
    or a["pool_status_class"] in ("fail", "warn")
)
pet_count_total = sum(a["pet_count"] for a in audited)

# Subject line tells you everything you need at a glance
if not audited:
    subject = f"White Sands · {today:%a %b %d} — no check-ins"
elif problem_count:
    subject = f"⚠️ White Sands · {today:%a %b %d} — {len(audited)} check-in(s), {problem_count} ISSUE(S)"
else:
    subject = f"White Sands · {today:%a %b %d} — {len(audited)} check-in(s), all clean"


def _evidence_block(ev: dict | None) -> str:
    """Render a small italic snippet under a status cell when message evidence exists."""
    if not ev:
        return ""
    sender_label = "You" if ev.get("sender") == "host" else (ev.get("sender_name") or "Guest")
    when = ""
    if ev.get("created_at"):
        # ISO timestamps look like 2026-05-06T12:50:59Z — keep just the date for compactness
        when = " · " + ev["created_at"].split("T")[0]
    return (
        f'<div style="margin-top:6px;font-size:11px;color:#475569;background:#F8FAFC;'
        f'padding:6px 8px;border-radius:4px;border-left:2px solid #CBD5E1;font-style:italic;">'
        f'<span style="color:#0F172A;font-weight:600;font-style:normal;">{html_escape(sender_label)}{when}:</span> '
        f'"{html_escape(ev["snippet"])}"</div>'
    )


def render_row(a: dict) -> str:
    pet_badge = (
        f'<span style="background:#FEF3C7;color:#92400E;padding:2px 8px;border-radius:4px;'
        f'font-size:11px;font-weight:700;">🐾 {a["pet_count"]}</span>'
        if a["pet_count"] > 0 else
        '<span style="color:#94A3B8;">—</span>'
    )
    pet_color = {"ok": "#10B981", "ok-muted": "#94A3B8", "warn": "#F59E0B", "fail": "#DC2626"}[a["pet_status_class"]]
    pool_color = {"ok": "#10B981", "ok-muted": "#94A3B8", "warn": "#F59E0B", "fail": "#DC2626"}[a["pool_status_class"]]
    issue_badge = (
        f'<div style="margin-top:6px;font-size:12px;color:#DC2626;background:#FEE2E2;'
        f'padding:4px 8px;border-radius:4px;display:inline-block;">⚠️ {a["issue_alert"]}</div>'
        if a.get("issue_alert") else ""
    )
    pet_evidence_html = _evidence_block(a.get("pet_msg_evidence"))
    pool_evidence_html = _evidence_block(a.get("pool_msg_evidence"))
    return f"""
<tr>
  <td style="padding:14px 12px;border-bottom:1px solid #E2E8F0;vertical-align:top;">
    <div style="font-weight:700;font-size:15px;color:#0F172A;">{a['name']}</div>
    <div style="font-size:12px;color:#64748B;margin-top:2px;">
      {a['code']} · {a['platform']} · {a['property_name']}
    </div>
    <div style="font-size:12px;color:#64748B;margin-top:2px;">
      {a['party_total']} guests ({a['adult']}A + {a['child']}K{f" + {a['infant']}I" if a['infant'] else ''}) · {a['nights']} nights → {a['departure']}
    </div>
    {issue_badge}
  </td>
  <td style="padding:14px 8px;border-bottom:1px solid #E2E8F0;vertical-align:top;text-align:center;">
    <div style="font-size:18px;font-weight:700;color:#0F172A;">{a['checkin_time']}</div>
    <div style="font-size:11px;color:#94A3B8;text-transform:uppercase;letter-spacing:1px;">check-in</div>
  </td>
  <td style="padding:14px 8px;border-bottom:1px solid #E2E8F0;vertical-align:top;text-align:center;">
    {pet_badge}
  </td>
  <td style="padding:14px 12px;border-bottom:1px solid #E2E8F0;vertical-align:top;font-size:13px;color:{pet_color};">
    {a['pet_status']}
    {pet_evidence_html}
  </td>
  <td style="padding:14px 12px;border-bottom:1px solid #E2E8F0;vertical-align:top;font-size:13px;color:{pool_color};">
    {a['pool_status']}
    {pool_evidence_html}
  </td>
</tr>"""


if not audited:
    body_inner = """
<div style="background:#F0FDFA;border-left:4px solid #00B4B4;padding:20px;border-radius:0 8px 8px 0;margin-top:24px;">
  <div style="font-size:18px;font-weight:600;color:#0F172A;">No check-ins today.</div>
  <div style="font-size:14px;color:#475569;margin-top:6px;">
    Cleaners can take it easy. Have a good one.
  </div>
</div>
"""
else:
    rows_html = "\n".join(render_row(a) for a in audited)
    issues_summary = ""
    if problem_count:
        issues_summary = f"""
<div style="background:#FEF2F2;border-left:4px solid #DC2626;padding:14px 18px;border-radius:0 8px 8px 0;margin:20px 0;">
  <strong style="color:#991B1B;">{problem_count} fee issue(s) flagged below.</strong>
  <span style="color:#475569;"> Audit each one before the guest arrives.</span>
</div>
"""
    body_inner = f"""
<div style="display:flex;gap:24px;margin-top:8px;font-size:14px;color:#475569;">
  <div><strong style="color:#0F172A;">{len(audited)}</strong> check-in{'s' if len(audited)!=1 else ''}</div>
  <div><strong style="color:#0F172A;">{pet_count_total}</strong> pet{'s' if pet_count_total!=1 else ''}</div>
  {f'<div><strong style="color:#DC2626;">{problem_count}</strong> issue{"s" if problem_count!=1 else ""}</div>' if problem_count else ''}
</div>
{issues_summary}
<table style="width:100%;border-collapse:collapse;margin-top:20px;background:white;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.06);">
  <thead>
    <tr style="background:#F1F5F9;">
      <th style="padding:10px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#64748B;">Guest</th>
      <th style="padding:10px 8px;text-align:center;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#64748B;">Time</th>
      <th style="padding:10px 8px;text-align:center;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#64748B;">Pets</th>
      <th style="padding:10px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#64748B;">Pet fee</th>
      <th style="padding:10px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#64748B;">Pool heating</th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
"""

html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#FAFAF7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#0F172A;">
<table role="presentation" style="width:100%;border-collapse:collapse;">
  <tr>
    <td style="padding:32px 16px;">
      <table role="presentation" style="max-width:720px;margin:0 auto;border-collapse:collapse;">
        <tr><td>
          <div style="border-bottom:3px solid #00B4B4;padding-bottom:16px;margin-bottom:24px;">
            <div style="font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#00B4B4;font-weight:700;">Hospitable Daily Brief</div>
            <h1 style="margin:8px 0 4px;font-family:Georgia,serif;font-size:30px;font-weight:400;color:#0F172A;">
              {today:%A, %B %-d}
            </h1>
            <div style="font-size:13px;color:#94A3B8;">Generated {local_now:%-I:%M %p %Z}</div>
          </div>
          {body_inner}
          <div style="margin-top:32px;padding-top:20px;border-top:1px solid #E2E8F0;font-size:12px;color:#94A3B8;">
            Audit rules: pet fee = ${PET_FEE_PER_PET:.0f}/pet/stay · pool heating = ${POOL_HEATING_PER_NIGHT:.0f}/night.
            Source: Hospitable financials API.
          </div>
        </td></tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""

# Plain-text fallback for clients that don't render HTML
def render_text() -> str:
    if not audited:
        return f"Hospitable Daily Brief — {today:%A, %B %d, %Y}\n\nNo check-ins today."
    lines = [
        f"Hospitable Daily Brief — {today:%A, %B %d, %Y}",
        f"Generated {local_now:%I:%M %p %Z}",
        "",
        f"{len(audited)} check-in(s) · {pet_count_total} pet(s)" + (
            f" · {problem_count} ISSUE(S)" if problem_count else ""
        ),
        "",
    ]
    for a in audited:
        lines += [
            f"━━━ {a['checkin_time']} — {a['name']} ({a['code']}) ━━━",
            f"  {a['property_name']} · {a['platform']} · {a['nights']} nights → {a['departure']}",
            f"  Party: {a['party_total']} ({a['adult']}A + {a['child']}K"
            + (f" + {a['infant']}I" if a['infant'] else "") + ")",
            f"  Pets:  {a['pet_count']}  →  {a['pet_status']}",
        ]
        if a.get("pet_msg_evidence"):
            ev = a["pet_msg_evidence"]
            who = "You" if ev.get("sender") == "host" else (ev.get("sender_name") or "Guest")
            lines.append(f'         msg ({who}): "{ev["snippet"]}"')
        lines.append(f"  Pool:  {a['pool_status']}")
        if a.get("pool_msg_evidence"):
            ev = a["pool_msg_evidence"]
            who = "You" if ev.get("sender") == "host" else (ev.get("sender_name") or "Guest")
            lines.append(f'         msg ({who}): "{ev["snippet"]}"')
        if a.get("issue_alert"):
            lines.append(f"  ⚠️  ALERT: {a['issue_alert']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Send via Resend
# ---------------------------------------------------------------------------

print(f"Sending email to {EMAIL_TO} via Resend...")
payload = json.dumps({
    "from": EMAIL_FROM,
    "to": [EMAIL_TO],
    "subject": subject,
    "html": html,
    "text": render_text(),
}).encode("utf-8")

req = urllib.request.Request(
    "https://api.resend.com/emails",
    data=payload,
    headers={
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "hospitable-daily-brief/1.0",
    },
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8")
    print(f"  Resend OK: {body}")
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")
    print(f"  Resend FAILED ({e.code}): {body}", file=sys.stderr)
    sys.exit(1)

print("Done.")
