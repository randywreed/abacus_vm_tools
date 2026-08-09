---
name: abacus-vm-web-app
description: Implement and publish HTTP apps through an Abacus VM.
version: 0.1.0
author: Randy Reed, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Abacus, VM, web, implementation, deployment]
    related_skills: []
---

# Abacus VM Web App Skill

Use this skill when a user asks you to implement, run, or publish an HTTP web
application on an Abacus SuperComputer VM. It covers the complete loop: inspect
the project, implement the requested app, run it on loopback, register it with
the per-VM classroom connector, and verify the public URL. Do not stop after
writing files or describing commands; exercise the running app and report real
results.

## When to Use

Use when the user asks for a dashboard, demo, site, visualization, API, or other
HTTP app that should be reachable from a browser through the VM hostname.

Do not use this skill to expose Hermes, the connector, databases, debuggers,
private credentials, arbitrary remote hosts, or broad VM services.

## Prerequisites

- The connector from this repository is installed and healthy.
- The project directory and its manifest have been inspected before choosing a
  build or start command.
- The app has a stable lowercase name such as `game` or `class-demo`.
- Choose an unused unprivileged port from `1024–65535`, excluding connector and
  infrastructure ports.
- The VM's public HTTPS hostname is known from the Abacus console.

## External URL and Base Path

The public URL is:

```text
https://<vm-host>/hermes-classroom/apps/<app-name>/
```

For an app named `class-demo`, configure the app's external base path as:

```text
/hermes-classroom/apps/class-demo/
```

Apply the equivalent setting for the detected framework: for example, Vite's
`base`, a browser router's `basename`, or the framework's documented public
path setting. Keep asset, API, and client-router requests under this prefix.
Prefer relative URLs where practical. The connector removes the prefix before
forwarding to the app, so the upstream app receives paths such as `/` and
`/assets/app.js`.

## Procedure

1. **Inspect before editing.** Use `read_file` and `search_files` to identify the
   project manifest, existing entrypoint, start scripts, router, asset settings,
   and tests. Preserve the project's framework and conventions; do not invent
   dependencies or replace a working app unnecessarily. Completion criterion:
   you can name the files and command that will run the app.

2. **Implement the requested behavior.** Make the smallest coherent change that
   produces the requested UI/API. Add or update focused tests when the project
   has a test convention. Keep secrets and private administration out of the
   published app. Completion criterion: the required source files and tests are
   written, and the relevant build/test command is known.

3. **Configure the public prefix.** Set the detected framework's base path or
   router basename to `/hermes-classroom/apps/<app-name>/`. Do not hardcode the
   VM hostname into source files. Completion criterion: generated asset and
   browser-router URLs are expected to remain under the app prefix.

4. **Start on loopback.** Use the `terminal` tool to run the project's existing
   start command with the app bound to `127.0.0.1:<port>`. For a long-running
   process, use `terminal(background=True)` and inspect it with `process`; do
   not blindly claim that it started. Completion criterion: a local `curl` or
   equivalent request returns the app's expected response.

5. **Register after the port is listening.** Use the `terminal` tool:

   ```bash
   curl -sS -X POST \
     http://127.0.0.1:8765/hermes-classroom/v1/apps \
     -H 'Content-Type: application/json' \
     -d '{"name":"class-demo","port":8767}'
   ```

   A successful response contains:

   ```json
   {"ok":true,"name":"class-demo","port":8767,"url":"/hermes-classroom/apps/class-demo/"}
   ```

   Registration is loopback-only and must not be sent through the public
   hostname. Completion criterion: the response says `"ok":true` with the
   expected name and port.

6. **Verify public routing.** Construct the full URL from the VM hostname and
   the relative path in the registration response. Check the public response
   and inspect browser/network requests for prefix escapes or missing assets.
   Completion criterion: the public URL returns the implemented app, not merely
   the connector's registration response.

7. **Report the result.** Tell the user what was implemented, the real start
   command, the loopback port, the public URL, the verification output, and any
   process handle needed to stop it. If the process or connector restarts,
   repeat registration because the registry is in-memory.

## Multiple Applications

Each VM can register up to eight named applications. Names use lowercase ASCII
letters, digits, and internal hyphens, 1–63 characters. Re-registering an
existing name updates its port.

Example registrations:

```text
game       -> 127.0.0.1:8767
portfolio  -> 127.0.0.1:8770
```

Never use `8642` (Hermes), `8765` (connector), or infrastructure ports such as
`2375/2376`, `3306`, `5432`, `6379`, `9229`, `11211`, or `11434`.

## Supported Traffic and Safety

The proxy supports ordinary HTTP `GET`, `HEAD`, `POST`, `PUT`, `PATCH`,
`DELETE`, and `OPTIONS` requests, including query strings and streamed HTTP
responses. Request bodies are capped at 1 MiB, and proxy slots are held for the
full upstream response lifetime.

WebSocket upgrades are not supported. The HTTP proxy does not forward upgrade
requests and strips hop-by-hop `Upgrade` headers. Use ordinary HTTP polling or
test SSE explicitly for realtime behavior.

The proxy target is always `127.0.0.1:<registered-port>`; registration cannot
select an arbitrary hostname. The public app path is not an application-login
boundary, so do not publish secrets, debug consoles, or administrative
mutations through it.

## Troubleshooting and Verification

- Local app fails: confirm the process is alive and listening on the selected
  loopback port before registering.
- Public `404`: check the exact lowercase app name and register again.
- Public `502`: check the process, loopback bind address, and selected port.
- Assets or client routes escape the prefix: correct the framework base path or
  router basename; do not add a new firewall port.
- After connector or VM restart: repeat registration from the app startup path.

A complete task has all of these observable results: the implementation exists,
the app starts, local HTTP works, registration returns `ok:true`, the public URL
works, and the reported URL uses the VM's actual hostname.
