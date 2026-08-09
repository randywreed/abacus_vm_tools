---
name: abacus-vm-web-deployment
description: Publish multiple HTTP apps through an Abacus VM hostname.
version: 0.1.0
author: Randy Reed, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Abacus, VM, web, deployment, connector]
    related_skills: []
---

# Abacus VM Web Deployment Skill

Use the per-VM classroom connector as the public router for web applications
running on an Abacus SuperComputer. Each VM has its own public HTTPS hostname;
the connector maps multiple named applications to loopback ports on that VM.
This skill covers application registration and verification. It does not manage
the application's process lifecycle, build a frontend, or expose Hermes itself.

## When to Use

Use when an HTTP web app running on this VM needs a public URL.

Do not use this procedure to expose Hermes, the connector, databases, debuggers,
or arbitrary remote hosts.

## Prerequisites

- The connector from this repository is installed and healthy.
- The application is running on the VM and listens on `127.0.0.1`.
- The application uses an unprivileged, non-reserved port, normally `1024–65535`.
- The VM's public HTTPS hostname is known from the Abacus console.
- The app has a stable lowercase name such as `game` or `class-demo`.

## Public URL

The public URL is:

```text
https://<vm-host>/hermes-classroom/apps/<app-name>/
```

For example, an app named `game` on the VM whose hostname is
`4100ca910.abacusai.cloud` is:

```text
https://4100ca910.abacusai.cloud/hermes-classroom/apps/game/
```

The `/hermes-classroom/apps/<app-name>` prefix is removed before the request
reaches the app. The upstream app therefore receives `/`, `/assets/app.js`, and
similar paths.

## Frontend Base Path

Configure the app for the external prefix. Otherwise root-relative assets and
client-side routes will escape the tunnel:

```text
/hermes-classroom/apps/<app-name>/
```

For Vite, set `base` to that value. For a browser router, set its basename to
the same value. Prefer relative asset and API URLs when practical.

## Registration

Start the app first, then use the `terminal` tool to register it locally:

```bash
curl -sS -X POST \
  http://127.0.0.1:8765/hermes-classroom/v1/apps \
  -H 'Content-Type: application/json' \
  -d '{"name":"game","port":8767}'
```

A successful response contains the relative public URL:

```json
{"ok":true,"name":"game","port":8767,"url":"/hermes-classroom/apps/game/"}
```

Registration, listing, and deletion are loopback-only management operations.
Do not call them through the public hostname; those requests are intentionally
rejected.

List registrations:

```bash
curl -sS http://127.0.0.1:8765/hermes-classroom/v1/apps
```

Remove one:

```bash
curl -sS -X DELETE \
  http://127.0.0.1:8765/hermes-classroom/v1/apps/game
```

## Multiple Applications

Each application gets a unique validated name and port:

```text
game       -> 127.0.0.1:8767
portfolio  -> 127.0.0.1:8770
```

The registry supports up to eight applications per VM. Re-registering an
existing name updates its port. Registrations are in-memory and disappear when
the connector or VM restarts, so every application's startup procedure must
register again after confirming that its port is listening.

## Supported Application Traffic

The proxy supports ordinary HTTP `GET`, `HEAD`, `POST`, `PUT`, `PATCH`,
`DELETE`, and `OPTIONS` requests, including query strings and streamed HTTP
responses. Request bodies are capped at 1 MiB, and each bounded proxy slot is
held for the full upstream response lifetime.

WebSocket upgrades are not supported: the HTTP proxy does not forward upgrade
requests, and any HTTP `Upgrade` header is stripped before proxying.
Use ordinary HTTP polling or test SSE explicitly for realtime behavior.

## Port and Name Safety

- Names use lowercase ASCII letters, digits, and internal hyphens only.
- Names are 1–63 characters and cannot contain dots, slashes, spaces, or
  uppercase letters.
- Never register infrastructure ports including `8642` (Hermes), `8765`
  (connector), `2375/2376` (Docker), `3306` (MySQL), `5432` (Postgres), `6379`
  (Redis), `9229` (Node inspector), `11211` (memcached), or `11434` (Ollama).
- The proxy target is always `127.0.0.1:<registered-port>`; no hostname is
  accepted, so the app registration cannot be used as an arbitrary SSRF proxy.

The public app path itself is not an application login boundary. Do not put
secrets, private credentials, debug consoles, or administrative mutations in an
app that is published this way.

## Verification

Use the `terminal` tool and verify each item:

1. The app is listening on the selected loopback port.
2. Local registration returns `"ok":true` and the expected name/port.
3. A local request to the app returns the expected HTML or API response.
4. The public URL returns the app's response.
5. Browser developer tools show asset and client-router requests staying under
   `/hermes-classroom/apps/<app-name>/`.
6. After restarting the VM or connector, registration is repeated successfully.

If the public URL returns `404`, check the app name and re-register it. If the
public URL returns `502`, check that the app process is alive and listening on
`127.0.0.1:<registered-port>`. Do not open a new VM firewall port or expose the
connector directly.
