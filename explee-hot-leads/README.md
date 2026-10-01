# Explee hot leads → your inbox

Every 10 minutes your TransIP server asks Explee for new **hot leads**. Each one is
emailed to you on its own: name, job title, company, website, email, phone, country,
LinkedIn, when they became hot, and their reply word for word.

You never copy-paste anything, and you never get the same lead twice.

---

## Setting it up (about 10 minutes, once)

### Step 1: Make a Gmail "app password"

The server sends the emails through your Gmail account. Google needs a special
password for that, which is not your normal one.

1. Open **https://myaccount.google.com/apppasswords** and sign in.
   - If Google says app passwords aren't available, first turn on **2-Step Verification**
     at https://myaccount.google.com/security, then open the link again.
2. Under **App name**, type `explee` and click **Create**.
3. Google shows 16 letters in a yellow box. **Keep this window open.** You'll paste them
   in step 3.

### Step 2: Open your server's terminal

Log in to your TransIP server the way you normally do:

- **From your computer:** open Terminal (Mac) or PowerShell (Windows) and type
  `ssh root@YOUR-SERVER-IP`, or
- **From the browser:** go to the TransIP control panel, open your VPS, and click
  **Console**.

### Step 3: Paste these two lines

Copy both lines, paste them into the server terminal, and press Enter:

```bash
curl -fsSLO https://raw.githubusercontent.com/imyuliy/CL4R1T4S/refs/heads/claude/explee-hot-leads-crm-fisit0/explee-hot-leads/install.sh
sudo bash install.sh
```

It asks you five things:

| It asks | You answer |
|---|---|
| Explee API key | Paste your key (it starts with `sk_explee_`). Nothing appears while you paste; that's normal. Press Enter. |
| Gmail address that sends the emails | Your Gmail address |
| Gmail app password | The 16 letters from step 1 (spaces don't matter) |
| Send the hot leads to which address? | Press Enter for the same Gmail, or type another address (e.g. your Vayu Solutions email) |
| Also email the hot leads from the past 7 days? | Press Enter for yes, or type `n` to only get new ones from now on |

It then checks the Explee connection and sends you a **test email**. If both show `OK`,
it switches on the 10-minute schedule. You're done.

> **Don't see the test email?** Look in your spam folder and mark it **Not spam**.
> After that, the hot lead emails will land in your inbox.

---

## Day to day

Nothing to do. New hot leads arrive within about 10 minutes, with the subject
**"Hot lead: Name (Company)"**. To answer a lead, click the orange **Email <name>** button
in the message, or start a new email to their address. Replying to the notification
itself only goes back to you, never to the lead.

**If something breaks** (for example, the Explee key stops working), you get **one**
email titled *"Explee hot leads: sync is failing"* after about an hour of failures,
and another one when it works again. No lead is lost while it's down: once it's
fixed, every missed lead is sent.

### Handy commands (on the server)

| What | Command |
|---|---|
| Is it working? | `sudo explee-hot-leads status` |
| Send a test email and check Explee | `sudo explee-hot-leads test` |
| See what it did recently | `sudo journalctl -u explee-hot-leads -n 50` |
| Change a setting (key, email, …) | `sudo nano /etc/explee-hot-leads/config.env` (takes effect on the next run) |
| Update to a newer version | run the two lines from step 3 again and choose *Keep settings* |
| Remove it completely | `sudo bash install.sh --uninstall` |

### Privacy

Your Explee key and Gmail app password are stored **only on your server**, in
`/etc/explee-hot-leads/config.env`. Only the server's administrator and this program can
read that file. They're never put in GitHub.

---

## How it works (technical notes)

- **Files:** `explee_hot_leads.py` (Python 3.8+, standard library only, nothing to
  `pip install`), `install.sh` (sets up a `systemd` timer that runs every 10 minutes,
  or cron if the server has no systemd), and `test_explee_hot_leads.py` (run with
  `python3 -m unittest -v test_explee_hot_leads`).
- **Request:** `GET https://api.explee.com/public/api/v1/autogtm/hot-leads?since=…&project_id=42547`
  with the `X-API-Key` header.
- **Position (cursor):** the newest `became_hot_at` that was emailed is saved in
  `/var/lib/explee-hot-leads/state.json` after **every** email. So a crash or mail
  outage never skips or repeats a lead. Leads are handled oldest first, so the cursor
  can't move past a lead that hasn't been sent yet.
- **Overlap + dedupe:** each run asks from *cursor − 60 minutes*, so a lead Explee
  stores a little late isn't missed. Leads that were already emailed are recognised by
  their id (or email + `became_hot_at`) and skipped. That also covers an inclusive `since`.
- **Fail closed:** `401`/`403` stops immediately with a clear message. `429`/`5xx` are
  retried three times. A response shape it doesn't recognise is treated as an error,
  never as "no leads". The installer only turns on the schedule after the live check
  passes.
- **First run:** looks back 7 days (or 0 if you answered `n`); the setting is
  `FIRST_RUN_LOOKBACK_DAYS`.
- **Overlapping runs:** a lock file stops two runs from running at the same time.
- **Email security:** sending requires TLS (STARTTLS on 587, or SSL on 465), so the
  password is never sent unencrypted.
- **Other mail providers:** set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
  and `MAIL_FROM` in the config file (for example, a TransIP mailbox:
  `smtp.transip.email`, port `465`).

### Known risk (to confirm against Explee's API docs)

The code assumes Explee returns **all** hot leads since `since` in one answer, or
marks extra pages with `has_more`. Suppose Explee instead caps the list, returns the
**newest first**, and gives no `has_more` flag. Then a burst of more leads than the cap
within 10 minutes could skip the oldest ones in that burst. This can be confirmed or
fixed once `https://api.explee.com/public/api/openapi.json` has been checked.
