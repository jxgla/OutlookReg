<h1 align="center">OutlookRegister</h1>

<p align="center">
  Automated Outlook / Hotmail registration and Microsoft Graph OAuth2 <code>refresh_token</code> collection (browser automation via DrissionPage driving your local Chrome).
</p>

<p align="center">
  <a href="./README.md"><b>English</b></a> ·
  <a href="./README.zh-CN.md">简体中文</a> ·
  <a href="./README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-green.svg">
  <img alt="DrissionPage" src="https://img.shields.io/badge/Browser-DrissionPage-4B5563">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-blue">
</p>

> Community fork based on **[LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister)**.  
> This fork focuses on registration + OAuth2 hardening: recovery-email binding (optional), multi-stage login challenges, cookie-first OAuth, batching, and interrupt-safe summaries.

---

## Getting started

### 1. Requirements

- Python 3.10+ recommended  
- A working HTTP/SOCKS proxy (strongly recommended)  
- Optional: a temp-mail / CF Temp Mail compatible API if you enable recovery-email binding  

### 2. Install

```bash
git clone <this-repo-url>
cd OutlookRegister
pip install -r requirements.txt
# No separate browser download needed; just ensure Google Chrome is installed.
# If Chrome is not on the default path, set browser.path in config.json.
```

### 3. Configure

```bash
# Windows
copy config.example.json config.json

# Linux / macOS
cp config.example.json config.json
```

Edit `config.json`: set **proxy** at minimum. Leave secrets empty in any copy you publish.

### 4. Run

```bash
python main.py
```

Successful tokens are **appended** to `Results/oauth2.txt`:

```text
email----password----client_id----refresh_token
```

Logs are written under `log/`. Press **Ctrl+C** to stop; the process writes a summary, then cleans browsers/profiles.

---

## `config.json` field reference

Ship template: `config.example.json` (same shape as the empty `config.json`). Both accept whole-line `//` comments (the loader strips them before parsing; a `.vscode/settings.json` maps them to JSONC so the editor won't flag them).

### Top-level

| Field | Type | Description |
| --- | --- | --- |
| `email_suffix` | string | Mail domain suffix used when generating accounts, e.g. `@outlook.com` or `@hotmail.com`. |
| `headless` | bool | `false` = show browser windows; `true` = headless. |
| `bot_protection_wait` | number | Base pacing for form fill (seconds). Code multiplies by 1000 for delays. |
| `max_captcha_retries` | number | Total press-and-hold attempts for the captcha (e.g. `2` = after two failed holds the current window is dropped; in `single` proxy mode the run ends, marking the IP unusable). |
| `captcha_strategy` | number | Captcha / handoff mode (see table below). |
| `px_solve_mode` | string | PerimeterX press-and-hold solver: `"hold"` (default, steady success rate) or `"a11y"` (accessibility fallback: click the accessibility icon → let the progress bar fill → click the bar). |
| `concurrent_flows` | number | Concurrent worker threads (parallel browsers). |
| `tasks` | number | Global cap on submitted tasks. Stops when this or `success_tasks` is reached (whichever first). |
| `success_tasks` | number \| null | Global success cap. `null` = no success cap (still limited by `tasks`). |
| `batch_success_limit` | number | Successes per batch before resetting in-process proxy weights / stats and starting the next batch. Cumulative success/time keep counting. Does **not** change a fixed proxy’s real exit IP. |
| `proxy` | object | Proxy pool (required for real use). |
| `oauth2` | object | Graph OAuth2 settings. |
| `temp_mail` | object | Optional recovery-email binding via temp-mail API. |

### `captcha_strategy`

| Value | Behavior |
| --- | --- |
| `0` | Fully automatic (captcha + mailbox + OAuth). |
| `1` | Semi-automatic: you press captcha; rest automatic. |
| `2` | Hand-off after captcha UI appears; no program OAuth for that task. |

### `proxy`

| Field | Type | Description |
| --- | --- | --- |
| `mode` | string | `single` = one port (`single_port`); `multiple` = port range `port_start`…`port_end`; `pool` = read proxies line by line from `pool_file` (auth socks5 supported, one proxy per window). |
| `type` | string | Proxy scheme, e.g. `http`, `socks5`. |
| `host` | string | Proxy host, e.g. `127.0.0.1`. **Fill this** before running. |
| `single_port` | number | Port when `mode` is `single`. |
| `port_start` | number | First port when `mode` is `multiple`. |
| `port_end` | number | Last port when `mode` is `multiple` (inclusive). |
| `max_per_proxy` | number | Max times a port may be selected before it is skipped (usage counters reset when all ports are exhausted or a batch resets). |

### `oauth2`

| Field | Type | Description |
| --- | --- | --- |
| `enable_oauth2` | bool | If `false`, registration success alone counts without fetching tokens. |
| `client_id` | string | Azure/public client id used for authorize + token exchange. |
| `redirect_url` | string | Redirect URI registered for the client (default `http://localhost`). |
| `Scopes` | string[] | OAuth scopes, typically `offline_access` + Graph default. |

### `temp_mail`

Used only if Microsoft shows **“Help us protect your account”** and binding is enabled.

| Field | Type | Description |
| --- | --- | --- |
| `enabled` | bool | `false` = never auto-bind recovery mail (skip path when the page appears). |
| `base_url` | string | Temp-mail API base URL (your deployment). Leave empty if unused. |
| `admin_password` | string | Admin password for creating addresses. **Do not publish.** |
| `domain` | string | Mail domain for new addresses. |
| `name_prefix` | string | Optional local-part prefix. |
| `enable_prefix` | bool | Whether to apply `name_prefix`. |
| `code_timeout` | number | Seconds to wait for a verification code email. |
| `poll_interval` | number | Seconds between inbox polls. |

---

## What it does

```text
Generate email/password
  -> open signup page
  -> fill form
  -> press-and-hold captcha
  -> (optional) bind recovery email if Microsoft asks
  -> enter Outlook mailbox
  -> OAuth2 (cookie-first, then new browser with injected cookies)
  -> append refresh_token to Results/oauth2.txt
```

OAuth handles common intermediate pages: personal/work account chooser, protect-account, email proof (or password path), KMSI “No”, consent, and code capture.

---

## Key features

- Concurrent multi-proxy registration  
- Batch success limit with in-process weight reset  
- Cookie-first OAuth + cold/new-browser fallback with `storage_state` when possible  
- Optional recovery-email binding via configurable temp-mail API  
- Ctrl+C: write interrupt summary, then close browsers and clear profiles  
- Failure breakdown and progress logs under `log/`  

---

## Upstream and acknowledgements

- [LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister) — original project this fork is based on  
- [Microsoft identity platform / Graph](https://learn.microsoft.com/en-us/graph/auth-v2-user) — OAuth2 and Graph scopes  
- [DrissionPage](https://github.com/g1879/DrissionPage) — browser automation (drives local Chrome)  

---

## License

Distributed under the [MIT License](./LICENSE).  
Attribution retained for upstream OutlookRegister and MIT-licensed portions this project builds on.

---

## Community

- [linux.do](https://linux.do): **Learn AI on L-Station!!!**
