# Hospitable Daily Check-in Brief

A GitHub Actions cron job that emails a daily check-in audit at **7:00 AM Central** every day. For each guest checking in today, it pulls the financials from Hospitable and audits whether:

- the **pet fee** was correctly collected (pet count × $150)
- **pool heating** was requested and paid ($50/night)

The summary lands in your inbox before cleaners arrive.

---

## What the email looks like

**Subject:** `White Sands · Mon May 6 — 1 check-in, all clean`

The body is a clean HTML table with one row per check-in showing guest name, time, party size, pet count, pet-fee status, and pool-heating status. Issues (missing or under-collected fees) are highlighted in red and called out in the subject line.

If there are no check-ins, you get a one-line "no check-ins today" email. (You can comment out that case in `daily_brief.py` if you only want emails on days with arrivals.)

---

## One-time setup (~5 minutes)

### 1. Create a private GitHub repo

```bash
# On your Mac
mkdir hospitable-daily-brief && cd hospitable-daily-brief
# Drop the four files from this folder in here:
#   daily_brief.py
#   .github/workflows/daily-brief.yml
#   .gitignore
#   README.md
git init
git add .
git commit -m "Initial commit"
gh repo create hospitable-daily-brief --private --source=. --push
```

(If you don't have the `gh` CLI: create the repo at <https://github.com/new>, mark it **Private**, then push manually.)

### 2. Get a Hospitable Personal Access Token (PAT)

1. Log in to <https://my.hospitable.com>
2. Click your profile → **Apps & Integrations** (or **Settings → API**)
3. **Personal Access Tokens** → **Create Token**
4. Name it `daily-brief`
5. Scopes needed: `properties:read`, `reservations:read`
6. Copy the token — you only see it once

### 3. Add secrets to your GitHub repo

GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these two:

| Name | Value |
|---|---|
| `HOSPITABLE_TOKEN` | The PAT from step 2 |
| `RESEND_API_KEY`   | Your Resend API key (`re_...`) |

### 4. (Optional) Override defaults via Variables

Same screen, **Variables** tab. Add any of these to override defaults:

| Variable | Default | What it does |
|---|---|---|
| `EMAIL_TO`         | `avulastays@gmail.com`  | Recipient address |
| `EMAIL_FROM`       | `onboarding@resend.dev` | Sender (Resend sandbox) |
| `SEND_WINDOW_HOUR` | `7`                     | Local hour to send (24-hr) |
| `SEND_TIMEZONE`    | `America/Chicago`       | IANA TZ for the window |

You don't need to set any of these unless the defaults don't match what you want.

### 5. Run it manually once to verify

GitHub repo → **Actions** tab → **Hospitable Daily Brief** → **Run workflow**.

Set "force_send" to `true` (default), click **Run workflow**. After ~30 seconds, check `avulastays@gmail.com` — the brief should arrive. If it doesn't, click into the failed run for logs.

### 6. Sit back

Cron fires automatically twice (UTC 12:00 and 13:00) so the script's local-time window (`6:30–7:30 AM Chicago`) catches exactly one fire per day, year-round, regardless of DST.

---

## Going beyond the sandbox sender

The default `EMAIL_FROM` is `onboarding@resend.dev` — Resend's sandbox sender. It works only when sending to the email you signed up Resend with (`avulastays@gmail.com` in your case).

To send from a custom address (`brief@yourdomain.com` or similar):

1. In Resend dashboard → **Domains** → **Add Domain**
2. Add the DNS records Resend gives you to your domain registrar
3. Wait for verification (~10 min)
4. Set `EMAIL_FROM=brief@yourdomain.com` as a GitHub Variable

---

## Troubleshooting

- **"Outside send window — skipping"**: The script ran but the local time wasn't 7 AM ±30 min. Cron fires twice daily for DST safety; the off-DST run intentionally skips. Use `Run workflow` with `force_send=true` to test outside the window.

- **Resend 422 error mentioning "domain"**: You're trying to send `from` an unverified domain. Either revert to `onboarding@resend.dev` or finish DNS verification.

- **Hospitable 401 error**: The PAT expired or was revoked. Generate a new one and update the `HOSPITABLE_TOKEN` secret.

- **Hospitable 403 error**: PAT is missing required scope. Recreate with `properties:read` and `reservations:read`.

- **Email goes to spam**: Add `onboarding@resend.dev` (or your custom sender) to your Gmail contacts, or click "Mark as not spam" on the first one.

- **Want to change what's audited**: Edit `daily_brief.py`. The pet-fee + pool-heating logic lives in the section marked `Audit each reservation`. Add new fee labels by extending the `find_fee(...)` calls.

---

## File structure

```
hospitable-daily-brief/
├── .github/
│   └── workflows/
│       └── daily-brief.yml      # GitHub Actions cron schedule
├── daily_brief.py               # The audit + email script
├── .gitignore
└── README.md                    # This file
```

No `requirements.txt` — the script uses only Python stdlib. Runs in <5 seconds.

---

## Cost

- **Resend free tier**: 3,000 emails/month, 100/day. You're using ~30/month.
- **GitHub Actions free tier**: 2,000 minutes/month for private repos. Each run takes ~30 seconds. You'll use ~15 min/month.
- **Hospitable API**: included in your existing subscription.

Total: **$0/month**.
