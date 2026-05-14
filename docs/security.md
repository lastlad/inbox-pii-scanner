# Security

The threat model for a v1 personal local-first scanner is narrow: the
tool runs on the user's own machine, accesses the user's own mailbox
read-only, and stores derived data locally. That narrowness drives
most of the design choices below.

## Threat model

**In scope:**

1. A malicious or compromised webpage in the user's browser exfiltrating
   the local API.
2. Another local user account on the same machine reading the data dir.
3. A stolen laptop with the data dir on a non-encrypted volume.
4. Bugs that broaden the OAuth scope or leak the OAuth token off-machine.
5. Bugs that mutate the user's Gmail mailbox.

**Out of scope:**

1. A sufficiently privileged attacker with root on the machine. (They
   own everything.)
2. Targeted prompt-injection attacks against the LLM-based detectors.
   The detectors are CPU-bound token classifiers with no tool-use
   surface; the worst an attacker can do via a poisoned attachment is
   manipulate the *findings* the user sees, not exfiltrate or mutate
   state.
3. Supply-chain compromise of Python packages we depend on.
4. Network-level attackers between the user and Gmail. TLS to
   `gmail.googleapis.com` is the floor.

## OAuth posture

- **Scope:** `https://www.googleapis.com/auth/gmail.readonly` only.
  Pinned in
  [`inbox_scanner/gmail/auth.py::GMAIL_SCOPES`](../inbox_scanner/gmail/auth.py).
  Never widened. Every Gmail call sits behind a service object built
  from credentials carrying only that scope, so a programming error
  that tried to call `messages.modify` or `messages.send` would fail
  at the API level with a scope error — there is no token in the
  process that would authorise the write.
- **Each user runs their own OAuth client.** The README walks them
  through creating a Cloud Console project, enabling the Gmail API,
  and downloading a Desktop-app OAuth client. Nothing is shared.
  Consequence: there's no possibility of one user's traffic routing
  through a server controlled by anyone but Google + themselves, and
  Google's "unverified app" warning is acceptable because there is no
  shared app to verify.
- **Token storage:** `token.json` in the data dir. File permissions
  inherit umask defaults (typically `0644` for a normal user). The
  refresh token grants long-lived read-only access until revoked from
  Google's account dashboard.

## Localhost-only by default

The FastAPI server binds `127.0.0.1` and ships with no authentication
([`server.py`](../inbox_scanner/server.py),
[`cli.py::serve`](../inbox_scanner/cli.py)). The defense is the bind
address: only processes on the same machine can reach the API.

This is acceptable because:

1. Single-user local tool by design.
2. The API is read-only — there's no `POST` route that could mutate
   state. The worst a same-machine adversary could do is read the
   findings they already have filesystem-level access to.
3. Adding auth here would conflict with the "single command, just
   works" goal without measurably improving the threat model.

`--host 0.0.0.0` prints a loud red banner before binding, but doesn't
refuse:

```text
⚠  Binding to 0.0.0.0:8765 — this exposes the scanner's read-only API
(and through it, your indexed PII spans) to anyone reachable on that
interface. There is no auth. Override only if you know what you're doing.
```

If you genuinely need remote access, put it behind an SSH tunnel
rather than exposing the port:

```sh
ssh -L 8765:127.0.0.1:8765 user@your-mac
# now visit http://127.0.0.1:8765 on your laptop
```

## Data at rest

The data dir contains:

- Raw attachment bytes from your inbox (`attachments/blobs/`)
- Extracted text from those attachments (`extracted/`)
- A SQLite DB with PII spans and per-message verdicts
- Your OAuth refresh token

**There is no encryption-at-rest in v1.** SQLCipher is in the [v2
backlog](IMPLEMENTATION_PLAN.md#out-of-scope-for-v1-captured-for-v2-backlog).
The README and CLAUDE.md both call this out and recommend FileVault
(macOS full-disk encryption), which is enabled by default on most
modern Macs.

**Why this is acceptable for v1:**

- The data dir mirrors what already lives in your Gmail account; the
  scanner doesn't introduce new categories of sensitive content.
- A stolen-laptop scenario without FileVault would already give the
  attacker access to your `~/Library/Mail/` / browser cookies / SSH
  keys / etc. — the scanner's data dir is not the weakest link.
- A second user on the same machine reading `~/<user>/.../` is bounded
  by POSIX permissions; we don't widen them.

**Why it's not acceptable forever:**

- Encrypted-at-rest is the correct default and we should ship it in
  v2.
- A user running on a multi-user machine would benefit from
  group-restrictive umask (`0700` on the data dir).

## Mutation guarantees

- **Read-only Gmail scope.** Verified by the scope string and the
  fact that nothing in the codebase imports or constructs a service
  with any other scope. The only `gmail.client` methods the scanner
  calls are `messages.list`, `messages.get`, and
  `messages.attachments.get` — all idempotent reads.
- **No FastAPI write routes.** Inspect
  [`server.py`](../inbox_scanner/server.py): every `@app.get`, no
  `@app.post`/`@app.put`/`@app.delete`. The API cannot modify any
  state, local or remote.
- **Reset is local-only.** `inbox-scanner reset` deletes files in the
  data dir. It never touches Gmail.

A user reviewing a flagged email is expected to:

1. Click "Open in Gmail" in the review UI.
2. Delete / archive / clean up in Gmail's own web interface.

The scanner does not orchestrate that cleanup. It deliberately stays
out of the write path.

## Logging hygiene

Structured logs in `logs/` contain event names and metadata
identifiers (`message_id`, `attachment_id`, `content_hash`, error
strings) but **not** the matched PII spans themselves. `span_text` is
in the SQLite DB, not in the log files.

Two exceptions worth flagging:

1. Errors during attachment processing log the raw exception message,
   which can include partial filenames or content hashes. Not full
   content, but not aggressively minimised either.
2. `gmail.sync` logs `composite_id` values (which include
   `gmail_attachment_id`). Those IDs expire and are useless to an
   attacker — but they are noisy in log files.

If you ever pipe logs off-machine, consider running them through a
filter that drops the noisier fields.

## Token & credential rotation

- **Rotating the OAuth client:** drop a new `credentials.json` into
  the data dir, then `inbox-scanner auth` again. The previous token is
  overwritten.
- **Revoking access:** visit <https://myaccount.google.com/permissions>
  and revoke the OAuth client. The next `sync` will fail with a
  `CredentialsMissing`-shaped error; run `auth` again to re-authenticate.
- **Wiping everything locally:** `inbox-scanner reset --all -y`. After
  that the only place the data lived was in your inbox to begin with.

## Browser-side considerations

The frontend loads Alpine.js and Tailwind from `cdn.tailwindcss.com`
and `unpkg.com` respectively. **This means a network observer can see
that you've loaded the UI**, but not what's in it (the API traffic is
localhost-only). The CDN scripts are subject to whoever controls those
hosts:

- A compromised CDN could inject JS that calls `/api/email/{id}` and
  ships responses to an attacker — but only if the user is *also*
  running the scanner with `--host` overridden to a routable address.
  In the default 127.0.0.1 posture, exfiltration via `fetch` doesn't
  work because the attacker can't reach the API in the first place.
- If you're paranoid, vendor Alpine + Tailwind locally and serve them
  from the FastAPI app. v2 polish item.

## What we *don't* defend against

- **A malicious attachment in your own inbox.** The scanner is
  Defense-In-Depth: it lets you find PII you shouldn't be storing
  in email so you can clean it up. It's not an antivirus.
- **Compromised Python dependencies.** We pin direct deps in
  `pyproject.toml` and lock transitives in `uv.lock`. If a transitive
  is compromised, we're in the same boat as every other Python
  project.
- **A malicious user on the same machine with the same login.** They
  already own the data; encryption-at-rest in v2 would help here.

## See also

- [Operations](operations.md) — file inventory and lifecycle.
- [Sync pipeline § rate limiting](sync-pipeline.md#concurrency-knobs)
  — defends against Gmail-side rate-limit / quota abuse.
