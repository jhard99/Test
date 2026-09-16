# Setting this up without a terminal

Everything here happens in a browser. GitHub runs the agent on its own machines
every Monday and emails you the digest; you never install anything.

Roughly 20 minutes, most of it waiting for Gmail.

---

## Step 1 — merge the pull request

1. Go to **https://github.com/jhard99/Test/pulls**
2. Open the pull request, scroll down, click **Merge pull request**, then
   **Confirm merge**.

## Step 2 — make `main` the branch that runs

The repository started empty, so its default branch is the feature branch.
Scheduled runs only ever use the *default* branch, so switch it:

1. **Settings** (top of the repo) → **General**
2. Under **Default branch**, click the ⇄ switch icon
3. Choose **main** → **Update** → confirm

## Step 3 — decide if the repo should be private

The digest gets committed into the repo each week. Right now the repo is
public, so those digests would be public too. Unless you want that:

**Settings** → **General** → scroll to the bottom → **Change repository
visibility** → **Make private** → confirm.

(Private repositories get 2,000 free Actions minutes a month. This uses about
five.)

## Step 4 — create a Gmail App Password

The agent needs to read your newsletters and send you mail. Gmail requires an
"App Password" for this — your normal password will not work.

1. Go to **https://myaccount.google.com/security** and turn on **2-Step
   Verification** if it isn't already on. (App Passwords don't exist without it.)
2. Go to **https://myaccount.google.com/apppasswords**
3. Type any name, e.g. `AI digest`, and click **Create**
4. Copy the 16-character password it shows you. **You cannot see it again**, so
   paste it somewhere temporarily.

> Using something other than Gmail? You need your provider's IMAP and SMTP
> server names instead — search "<provider> IMAP settings". The steps are
> otherwise identical.

## Step 5 — add your details as repository secrets

1. **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** for each row below: type the **Name**
   exactly as shown, paste the **Value**, click **Add secret**. Repeat.

| Name | Value |
|---|---|
| `IMAP_HOST` | `imap.gmail.com` |
| `IMAP_PORT` | `993` |
| `IMAP_USER` | your full Gmail address |
| `IMAP_PASSWORD` | the 16-character App Password from step 4 |
| `IMAP_FOLDER` | `INBOX` (or a label name — see step 7) |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your full Gmail address |
| `SMTP_PASSWORD` | the same App Password |
| `DIGEST_FROM` | your full Gmail address |
| `DIGEST_TO` | where the digest should arrive (your Gmail is fine) |

Add this one only if you have an Anthropic API key:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your key, starting `sk-ant-` |

Secrets are write-only — you'll never see the values again, but you can
overwrite them any time.

## Step 6 — no API key? Switch on the keyword mode

Skip this step if you added `ANTHROPIC_API_KEY`.

1. From the repo home page, click **config.yaml**
2. Click the **pencil** icon (top right of the file) to edit it
3. Find the line `  enabled: true` in the `llm:` section (about 35 lines down)
4. Change it to `  enabled: false` — keep the two spaces at the start
5. Scroll to the bottom → **Commit changes** → **Commit changes**

You'll get the week's news filtered, ranked and linked, without the written
summaries. Change it back to `true` whenever you get a key.

## Step 7 — point it at your newsletters (recommended)

Out of the box it scans your whole inbox for a list of known newsletter senders.
It works better if you give it a dedicated label:

1. In Gmail: **Settings** (gear) → **See all settings** → **Filters and Blocked
   Addresses** → **Create a new filter**
2. In **From**, list your AI newsletters separated by `OR`, for example:
   `importai OR tldr OR bensbites OR deeplearning.ai OR platformer`
3. **Create filter** → tick **Apply the label** → **New label** → name it
   `AI News` → **Create filter**
4. Back on GitHub: **Settings** → **Secrets and variables** → **Actions**,
   click the pencil next to `IMAP_FOLDER`, and set it to `AI News`
5. Then edit `config.yaml` as in step 6 and delete the whole `senders:` block
   under the `imap` source (the line `senders:` and the indented lines under it),
   so everything in that label is read.

Gmail filters only apply to *new* mail, so the first week may be thin.

## Step 8 — run it now instead of waiting for Monday

1. Click the **Actions** tab
2. If the page offers a button to enable Actions for this repository, click it
3. In the left sidebar click **Weekly AI digest**
4. Click **Run workflow** (right-hand side). A small panel appears:
   - **window_days** — leave at 7
   - **dry_run** — tick this for the first run, so nothing gets emailed yet
   - **no_llm** — tick this if you have no API key
5. Click the green **Run workflow** button

Refresh the page after a few seconds. A run appears; click into it to watch the
log. It takes 3–10 minutes, mostly fetching feeds politely.

**When it finishes:**
- Green tick → scroll to the bottom of the run page, under **Artifacts** there's
  a **digest** download. That's your digest as `.md` and `.html`. Open the
  `.html` file in a browser.
- Red X → click the failed step to expand its log. The error is usually in the
  last few red lines. Common ones are in the table at the bottom of this page.

## Step 9 — turn on the real thing

Once a dry run looks right, run it again with **dry_run unticked**. The digest
should arrive in your inbox within a minute of the run finishing.

From then on it runs itself **every Monday at 13:00 UTC** (9am New York, 6am
California). Nothing more to do.

To change the day or time, edit `.github/workflows/weekly-digest.yml` in the
web editor and change the `cron` line — `"0 13 * * 1"` is
minute 0, hour 13, any day of month, any month, weekday 1 (Monday). Always UTC,
so it shifts by an hour when daylight saving changes.

---

## When something goes wrong

GitHub emails you when a scheduled run fails, so you'll know without checking.

| What the log says | What it means |
|---|---|
| `SMTP login rejected` / `Username and Password not accepted` | The App Password is wrong, or you used your normal Gmail password. Redo step 4 and overwrite `SMTP_PASSWORD` and `IMAP_PASSWORD`. |
| `skipping, missing username, password` | An `IMAP_*` secret is missing or misspelled. Names are case-sensitive. |
| `no ANTHROPIC_API_KEY` / authentication errors from Claude | Either add the key as a secret, or do step 6 to run without one. |
| `nothing new since the last digest` | Everything it found was already in a previous digest. Normal if you run it twice in a day; a problem if you see it on a fresh week. |
| `giving up on https://…` for one or two feeds | That site changed its feed address or is temporarily down. Harmless — the run continues. If it persists, edit `config.yaml` and delete that source block. |
| `robots.txt disallows` | That site asks automated readers to stay away, and the agent respects it. Nothing to fix. |
| Run says "no items collected" | Every source failed — usually a typo in `config.yaml`. Check the most recent edit you made. |

## Changing things later

Everything is editable in the browser with the pencil icon:

- **`config.yaml`** — add or remove news sources, change how many stories the
  digest carries (`max_stories`), raise `min_importance` to `3` for a shorter
  and stricter digest, or switch `triage_model` to `claude-sonnet-5` to cut
  cost.
- **Secrets** — Settings → Secrets and variables → Actions.
- **Schedule** — the `cron` line in `.github/workflows/weekly-digest.yml`.

Every edit commits to the repo and takes effect on the next run.
