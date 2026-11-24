# OAuth configuration for the daily digest

## Two independent OAuth surfaces

| Surface | Credential storage / use | Exact Gmail scopes |
| --- | --- | --- |
| A. Gmail Reader | Authorized-user JSON at `GMAIL_AUTHORIZED_USER_FILE`; daily discovery and revision source reads via `google-auth` | `https://www.googleapis.com/auth/gmail.readonly` |
| B. Gmail Send Boundary | `gog` keyring for the explicit `GOG_ACCOUNT`, unlocked through the protected `GOG_KEYRING_PASSWORD` mechanism | `read-send`: `https://www.googleapis.com/auth/gmail.readonly` + `https://www.googleapis.com/auth/gmail.send` |

**Valid OAuth A does not imply valid OAuth B, and valid OAuth B does not imply
valid OAuth A.** They can use different clients/projects, even for the same
mailbox. Check and re-authorize each independently; never copy the send grant
into the Reader file or use Reader success as send-boundary evidence.

Keep the existing permission architecture and exact scopes. Do not add
`gmail.modify`, `https://mail.google.com/`, settings scopes, any full Gmail scope,
or extra Google services. Publishing does not require broader permissions.
The [gog read-only setup](gmail-setup.md) applies to a separate reader workflow.
Use authorized-user JSON for surface A and the read-send grant for surface B.

## OAuth audience and token lifetime

Check the OAuth audience, publishing status and Gmail scopes for each issuing project in Google Auth Platform. Keep account, client and project identifiers in protected local configuration.

For **External / Personal Use with a small known user group**, consider changing
**Testing → In production** in the identified project's Audience page during the
migration. Google describes a personal-use exception for fewer than 100 users. Confirm the
deployment qualifies and reassess verification if use grows or changes.
Publishing and verification are separate: an **unverified app warning** and
applicable user caps can remain after publishing without verification.
See [Google's verification exceptions](https://support.google.com/cloud/answer/13464323?hl=en)
and [Audience/publishing rules](https://support.google.com/cloud/answer/15549945).

For **Google Workspace / Internal**, separately confirm that the project belongs
to the organization and every runtime mailbox is an eligible organization member.
Use the Internal configuration and any required Workspace administrator approval;
do not convert it to External just to follow the personal-use path. Record the
status shown by that project, or not applicable if no publishing control is
exposed. Internal use is a separate verification exception; administrator access
policies still apply. A Workspace mailbox using an External project must follow
the External decision above. See the same Google Audience and exception guidance.

External/Testing refresh tokens with Gmail scopes expire after **seven days**;
the basic sign-in-only exception does not cover either surface here. Production
configuration removes that Testing-specific risk for newly issued grants, not
all causes of token failure: revocation, password changes, inactivity and token
limits still matter. Do not assume a previously issued Testing token has acquired
a new lifetime after publishing. See
[Google's refresh-token lifecycle](https://developers.google.com/identity/protocols/oauth2#expiration).

## Credential replacement checks

Pause scheduling and prevent new approvals before replacing credentials. Let active workers finish, preserve current state and back up credentials separately. Prepare the replacement files outside the checkout with owner-only access.

1. For the reader, obtain a fresh offline authorized-user credential scoped to `gmail.readonly`. Verify the issuing client, the actual granted scopes and the mailbox. A read-only Gmail profile request through the configured adapter can verify refresh and account binding.
2. For `gog`, obtain a fresh offline read-send grant for the intended account and client. Independently verify the actual scopes using the installed CLI's supported auth tooling. Check refresh with `gog auth list --check` and use the read-only metadata path to verify account access. Scope inspection verifies send permission without delivering a reply.
3. Replace each credential using its supported local storage mechanism. Check the installed credentials again in the service and plugin environments. Keep scheduling paused until the two checks pass. Review persistent timer catch-up before resuming.

Preserve `sent` and `send_uncertain` records when replacing or rolling back credentials. Inspect ambiguous deliveries in the Gmail thread and Sent folder. [Daily recovery](daily-sender-digest.md#recovery-and-evidence) · [Systemd templates](../deploy/systemd/README.md)
