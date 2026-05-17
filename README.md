# Inbox PII Scanner

A local tool that scans your Gmail inbox for emails with sensitive
attachments — IDs, financial documents, tax forms, credentials — and
shows you which ones to clean up.

Everything runs on your computer. Your inbox content never leaves your
machine. The tool can only **read** your mail, never modify it.

## Before you start

You need:

- A Mac (Apple Silicon recommended).
- A Gmail account.
- About **10 GB free disk space** — most of it for the AI models the
  scanner downloads on first use.
- A working terminal (the macOS **Terminal** app is fine).

The one-time setup takes about 20 minutes plus model download time.

## One-time setup

### 1. Install `uv`

`uv` is the package manager that runs everything for you. In Terminal:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Quit and reopen Terminal so the new tool is available.

### 2. Get the code

```sh
git clone <REPO_URL_HERE> inbox-pii-scanner
cd inbox-pii-scanner
uv sync
```

`uv sync` installs Python and all the libraries. It takes a couple of
minutes.

### 3. Create a Google OAuth client

The scanner connects to Gmail on **your** Google account. You create the
OAuth client yourself, so nothing routes through someone else's project.

> Heads-up: Google's Cloud Console wording changes from time to time.
> If something is labelled slightly differently, look for the closest
> match — the click order is what matters.

1. Open <https://console.cloud.google.com/projectcreate>. Sign in with
   the Google account whose mail you want to scan.
2. Name the project anything (e.g. `Inbox Scanner`) and click **Create**.
3. Wait for the green check mark, then make sure your new project is
   selected in the **project picker** at the top of the page.
4. Open <https://console.cloud.google.com/apis/library/gmail.googleapis.com>
   and click **Enable**.
5. Open <https://console.cloud.google.com/apis/credentials/consent>:
   - Choose **External** and click **Create**.
   - Fill in **App name**, **User support email**, and **Developer
     contact email** with your own info. Click **Save and continue**.
   - Skip the next two screens (just click **Save and continue**).
   - Click **Back to dashboard**.
6. Still on that screen, in the left sidebar click **Audience** →
   **Add users** and add your own Gmail address. Save.
7. Open <https://console.cloud.google.com/apis/credentials> and click
   **+ Create credentials** → **OAuth client ID**.
   - Application type: **Desktop app**.
   - Name: anything.
   - Click **Create**, then **Download JSON** in the popup.
8. Save the downloaded file as **`credentials.json`** inside the
   `.inbox-scanner-data/` folder of this project.

   The folder doesn't exist yet on a fresh checkout. Create it with:

   ```sh
   uv run inbox-scanner status
   ```

   That command sets up the folder layout (and reports nothing because
   you haven't synced anything yet). Then move `credentials.json` into
   `.inbox-scanner-data/`.

### 4. Sign in

```sh
uv run inbox-scanner auth
```

A browser window opens. Pick your Gmail account and approve the
**read-only** permission. The browser will say the authentication flow
has completed — you can close that tab.

You won't need to repeat this until the token expires (months later).

## Daily use

The scanner has three commands. Run them in this order the first time;
later you can re-run any of them as needed.

### Pull your inbox

```sh
uv run inbox-scanner sync
```

Downloads every email that has at least one attachment, and saves the
attachment files locally. Gmail rate-limits the connection, so a large
inbox takes a while: roughly 5 minutes per 5,000 messages.

You can stop with **Ctrl-C** and run it again later — it picks up where
it left off.

To test the setup with a small batch first:

```sh
uv run inbox-scanner sync --limit 20
```

By default sync pulls every message with an attachment — inbox, sent, and
archive. If you only care about what *you* sent (often the more sensitive
case — IDs sent to verify accounts, contracts to lawyers, forms to
accountants), narrow the scope:

```sh
uv run inbox-scanner sync --mailbox sent
```

`--mailbox inbox` is also available if you want received-only.

### Scan the attachments

```sh
uv run inbox-scanner scan
```

Reads the documents you just downloaded, extracts text from PDFs,
images, Word/Excel files, etc., and runs three PII detectors over the
text.

**The first run downloads about 5 GB of AI models.** This is a one-time
cost; after that, scans are fast (a couple of seconds per attachment).

You can re-run `scan` any number of times; it only touches your local
files, never Gmail.

By default the scan reports only **critical** PII — things whose leak
causes irreversible harm: Social Security numbers, passports, driver's
licenses, credit cards, bank/IBAN numbers, ITINs, API keys/passwords,
and crypto wallet mnemonics. To cast a wider net:

```sh
uv run inbox-scanner scan --profile standard   # adds account numbers + tax forms
uv run inbox-scanner scan --profile all        # additionally records names, addresses, emails, phones
```

Switching profiles requires a re-scan (results are rewritten each run).

### Review in your browser

```sh
uv run inbox-scanner serve
```

Opens a local web app at <http://127.0.0.1:8765>. The dashboard summarises
what was found. Click **Start review** to walk the flagged emails one at
a time.

In the review pane:

- **J** — next flagged email
- **K** — previous flagged email
- **O** — open the current email in Gmail (new tab)
- **Esc** — back to the dashboard

The scanner never modifies your mailbox. To delete or archive a flagged
email, do it in Gmail yourself.

When you're done reviewing, stop the server with **Ctrl-C** in the
terminal.

## Where your data lives

Everything is in the `.inbox-scanner-data/` folder inside the project:

| File / folder         | What's in it                                          |
|----------------------|-------------------------------------------------------|
| `credentials.json`   | Your Google OAuth client (you put this here in setup) |
| `token.json`         | Your saved sign-in token                              |
| `state.db`           | Local database of message metadata + PII findings     |
| `attachments/`       | Raw attachment bytes from your synced messages        |
| `extracted/`         | Text the scanner extracted from those attachments     |
| `logs/`              | Diagnostic logs                                       |

This folder contains your actual attachment data and the PII the scanner
identified. **Make sure FileVault is on** (System Settings → Privacy &
Security → FileVault). It's on by default on most modern Macs.

## Starting over

Most of the time you don't want to redo the Google sign-in. To wipe just
the local database and re-pull from Gmail:

```sh
uv run inbox-scanner reset
```

That keeps `token.json` and `credentials.json` and clears everything
else. It asks for confirmation; pass `-y` to skip the prompt.

Other useful variants:

| Command                                              | What it keeps                                                  |
|------------------------------------------------------|---------------------------------------------------------------|
| `inbox-scanner reset`                                | OAuth token + credentials                                     |
| `inbox-scanner reset --keep-attachments`             | Above + downloaded attachment files (skip Gmail re-download) |
| `inbox-scanner reset --keep-attachments --keep-extractions` | Above + extracted text cache (skip Docling re-run)    |
| `inbox-scanner reset --all`                          | Nothing — full nuke, including the OAuth setup               |

## Limits

This is a personal-scale tool. v1 caveats:

- **Gmail only.** No Outlook, iCloud, or Yahoo support.
- **US-focused.** It detects US SSNs, passports, driver's licenses,
  bank accounts, and tax-form titles. Generic things like names, emails,
  addresses, phone numbers, and credit/IBAN numbers are detected for
  any country.
- **Attachments only.** Email bodies are not scanned.
- **macOS only** is what's been tested. Linux likely works; Windows
  hasn't been tried.
