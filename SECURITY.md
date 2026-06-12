# Security Policy

Thanks for helping keep Memex and its users safe.

## Threat model

Memex is a **local-first, single-user** tool. The database, the embeddings, the
capture server, and the OAuth state all live on the user's own machine. The one
internet-facing surface is the optional remote MCP connector (`memex
serve-remote`), which is reachable only through the user's own tunnel and is
protected by GitHub OAuth with a per-request identity allow-list.

The properties we care most about:

- A third party cannot read the user's indexed chats from outside.
- Credentials that show up in captured terminal output or chats are redacted
  (best-effort) before being stored, embedded, or surfaced.
- Nothing is sent to any external service except, optionally, the Anthropic API
  for summaries (which the user enables explicitly).

## Reporting a vulnerability

**Please report privately, not in a public issue.**

Preferred: open a private report through GitHub's **"Report a vulnerability"**
button on the repository's **Security** tab (Security Advisories). This keeps the
details private until a fix is available.

Please include: what you found, how to reproduce it, the impact, and any
proof-of-concept. If you cannot use GitHub's private reporting, open a normal
issue that says only "security report, please enable private contact" without
any detail, and we will follow up.

What to expect:

- An acknowledgement of your report.
- An honest assessment of severity and whether it is in scope (see below).
- A fix or a documented mitigation, and credit if you would like it.

This is a small open-source alpha maintained by one person, so timelines are
best-effort, but security reports are taken seriously and prioritized.

## Scope

In scope (please report):

- Any way to read another user's indexed chats from outside their machine.
- Any auth bypass on the remote connector.
- A secret shape that survives redaction and reaches storage or the cloud.
- Remote code execution, or escaping the local trust boundary.

Known and out of scope (already understood, no need to report):

- Redaction is **best-effort** pattern matching, not a guarantee; a secret in an
  unusual or attacker-crafted format may slip through. We harden it continuously,
  but "I crafted an input that evades it" is expected for the long tail.
- Anything that requires already having code execution on the user's machine or
  in the claude.ai page (an attacker at that level already owns the data).
- Availability-only issues behind the user's own tunnel.

## Disclosure

We practice coordinated disclosure: we will work with you on a fix before any
public write-up, and we are happy to credit you in the changelog.
