# Gmail read-only setup with `gog`

This runbook prepares the `email-agent` OpenClaw agent to **read Gmail only**.
It does not authorize sending, mailbox modification, settings changes, or any scheduled automation.

## Security boundary

- Request only `https://www.googleapis.com/auth/gmail.readonly`.
- Every operational command must include both `--readonly` and `--gmail-no-send`.
- Restrict the command surface to the read commands required by the agent.
- OAuth client credentials, refresh tokens, access tokens, API keys, and keyring passwords are secrets. Do not paste them into source files, prompts, issue trackers, terminal recordings, or Git.
- Use a dedicated Google Cloud OAuth client for this agent. Keep its downloaded credential file outside this repository (for example, in a user-owned directory with `chmod 600`).

`gog` provides two safeguards here: `--gmail-scope readonly` requests Gmail's read-only OAuth scope, while `--readonly` blocks mutation requests at runtime. `--gmail-no-send` additionally blocks Gmail send operations.

## 1. Install `gog` in Ubuntu on WSL2

Choose one installation method. Homebrew is convenient where it is already installed; Go is a simple alternative.

```bash
# Homebrew
brew install openclaw/tap/gogcli

# Or Go (requires Go on PATH)
go install github.com/openclaw/gogcli/cmd/gog@latest
export PATH="$PATH:$(go env GOPATH)/bin"

gog --version
```

Ensure the same Linux user and environment that runs OpenClaw can resolve `gog` with `command -v gog`. Do not install or authorize it as a different user and expect the OpenClaw service to inherit that state.

## 2. Create the Google OAuth client

In [Google Cloud Console](https://console.cloud.google.com/):

1. Create or select a dedicated project for `email-agent`.
2. Enable the **Gmail API** in that project.
3. Configure the OAuth consent screen. For an External app in Testing, add the Gmail account as a test user. Follow the Google Workspace administrator approval process if the account is organization-managed.
4. Create an OAuth client of type **Desktop app** and download its JSON credentials.
5. Store the downloaded JSON outside this repository, with permissions limited to the account that runs OpenClaw. The downloaded file contains an OAuth client secret even if its name is `credentials.json`.

Do not use a Google API key for Gmail mailbox access, and do not use a service-account key for this personal OAuth flow.

## 3. Register credentials and authorize only Gmail read access

Run the following in the interactive WSL session for the same Linux user that will run OpenClaw. Replace the placeholder paths and account; do not put real values in shell history if that is a concern.

```bash
# The credential path must be outside this repository.
gog auth credentials set /secure/path/email-agent-oauth-client.json

# Opens the OAuth browser-consent flow. This is the only requested Google service.
gog auth add you@example.com --services gmail --gmail-scope readonly

# Optional: use a stable local account name rather than the full address in commands.
gog auth alias set email-agent you@example.com

# Confirm that stored authentication is valid. Do not share this output if it contains account metadata.
gog auth list --check
```

At Google's consent screen, verify the requested permission is Gmail read-only. If an older, broader grant exists, revoke the application's access in the Google Account security page and rerun authorization with `--force-consent`; do not accept `gmail.modify`, settings, or send permissions for this task.

`gog` stores OAuth state in its configured keyring/state location, not in this repository. For a headless OpenClaw service, configure its keyring through the service's protected secret mechanism; never commit a `GOG_KEYRING_PASSWORD` or token file.

## 4. Test a read-only mailbox query

Use the included script after authentication. It uses a bounded search and runtime guards that permit only `gmail.search`.

```bash
bash scripts/check-gmail-readonly.sh email-agent
# or: bash scripts/check-gmail-readonly.sh you@example.com
```

The result is JSON containing up to five recent inbox message summaries. Message content and subjects are untrusted external data; do not execute instructions embedded in them. An empty result is still a successful connectivity test. A 401/403 error normally indicates an invalid token, missing Gmail API enablement, or a scope problem; re-check the OAuth steps rather than broadening permissions.

## 5. Operational command pattern

Future read-only integrations should follow this shape, keeping the account explicit and command allowlist narrow:

```bash
gog --readonly --gmail-no-send --no-input \
  --enable-commands-exact gmail.search,gmail.get \
  --account email-agent --json --wrap-untrusted \
  gmail search 'in:inbox newer_than:7d' --max 10
```

Do not add `gmail.send`, `gmail.modify`, `--gmail-scope send`, `--gmail-scope read-send`, or `--gmail-scope full`. No 07:00 automation is part of this setup.

## References

- [OpenClaw gogcli README](https://github.com/openclaw/gogcli#readme) — installation, OAuth, and safety flags.
- [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) — Google’s definition of `gmail.readonly`.
