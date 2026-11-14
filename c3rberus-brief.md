# C3rberus — implementer-grade brief

Repo: `http://10.0.0.132:3000/hnasher1/c3rberus` (branch `main`, 1339 files). All citations are **repo-relative `file:line`**. Verified against code, not just docs.

**What it is:** a red-team *scoring* system for CDE cyber-defense competitions. NetExec (`nxc`) is the delivery/auth plane to target boxes, Sliver is the C2 plane for implant IOCs, a FastAPI backend runs a periodic IOC-check sweep and computes a live scoreboard, and a React UI displays it. Backend is fully drivable over HTTP; the frontend is optional.

---

## 1. Install on a fresh Ubuntu 24.04 VM

### 1.1 What the repo assumes

`README.md:9-20` and `wiki/Home.md:12-21` prescribe exactly two commands:

```bash
git clone <repo> && cd C3rberus
scripts/setup.sh      # apt + pip + npm + certs + sliver bootstrap
scripts/run.sh        # backend :3000 + frontend :5173
```

`scripts/setup.sh` (136 lines) does, in order:

| Lines | Action |
|---|---|
| `:24` | `mkdir -p db logs` |
| `:29-41` | `apt-get install` of `python3 python3-pip python3-venv nodejs npm openssl` (+ `DEBIAN_FRONTEND=noninteractive`); **full** mode adds `netexec mingw-w64 sliver` |
| `:44-53` | self-signed `certs/{cert.pem,key.pem}` via `openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=$(hostname)"` — **only if `certs/cert.pem` does not already exist** |
| `:56-66` | `python3 -m venv venv` + `venv/bin/pip install -r requirements.txt` |
| `:70` | `cd react && npm install` |
| `:73-76` | `npx playwright install chromium` (test-only; skipped in `--dev`) |
| `:82` | `sudo systemctl enable --now sliver` |
| `:85` | wait for TCP `:31337` (Sliver gRPC) |
| `:89-94` | `sudo /usr/bin/sliver-server operator --name c3rberus --lhost localhost --permissions all --save $PROJECT_DIR/sliver_operator.cfg` then `chown $USER` |
| `:97-101` | `sudo /usr/bin/sliver-server console <<< 'mtls -L 0.0.0.0 -l 8888'` |
| `:104-128` | in `--dev` mode only: boot uvicorn on `:3000` and run `scripts/testing/seed_dummy_data.py --clear --checks 10` |

Flags: `scripts/setup.sh --dev` (skip Playwright/Chromium + Sliver steps), `--full` (default).

### 1.2 Ubuntu 24.04 (noble) reality — where setup.sh breaks

Verified against the Ubuntu archive:

* `netexec` — **does not exist** as an Ubuntu package in noble or any series (Launchpad source-package search returns 0). It is a Kali package. `requirements.txt:12` even says *"netexec (nxc) is a system tool, not a pip package … Kali ships it pre-installed"*, and `lifespan_helpers.py:317-321` warns `nxc not found on PATH — install via: sudo apt install netexec` (advice that is dead on Ubuntu). Install it from source instead: `pip install git+https://github.com/Pennyw0rth/NetExec` (or a vendored clone) so `nxc` lands on `PATH`. The backend **degrades gracefully** without it — `init_nxc()` returns early (`lifespan_helpers.py:317-322`), but every NXC-path IOC check/deploy will then fail at run time.
* `sliver` — **does not exist** as an Ubuntu package. `wiki/sliver-setup.md:25` gives the correct route: `curl https://sliver.sh/install | sudo bash`, which installs `/usr/local/bin/sliver-server` + `/usr/local/bin/sliver-client`. Note two mismatches: that path is **not** `/usr/bin/sliver-server` (which `setup.sh:89` and `:97` hard-code), and it creates **no** `sliver.service` unit (which `setup.sh:82` enables and `sliver_manager.py:545-560` tries to `systemctl start`). Write your own unit — `ExecStart=/usr/local/bin/sliver-server daemon` (see §5.4).
* `nmap` (7.94) and `mingw-w64` (11.0.1) exist in noble universe. Good: `nmap` powers SA-reachability (`lifespan_helpers.py:398-411`), `x86_64-w64-mingw32-gcc` powers Windows shim builds (`c2/shim_builder.py:104-118`).
* `clang` is **required but not in setup.sh's apt list** — every FreeBSD shim compiles with `clang --target=x86_64-unknown-freebsd15 -fuse-ld=lld` (`c2/shim_builder.py:56,66,85,89,95`). Add `clang lld`.
* `python3` = 3.12.3. The code targets 3.13 (`ruff.toml:2 target-version="py313"`) while `CLAUDE.md:139` and `wiki/pre-competition.md:15` claim 3.14. Nothing I inspected uses 3.13+-only syntax, so 3.12 runs it; treat the "3.14" claim as aspirational and expect the odd `typing`-related surprise.
* `nodejs` = **18.19.1** — too old. `react/package.json` pins `vite ^7.1.2` (lockfile resolves 7.3.2, whose `engines` require `^20.19.0 || >=22.12.0`). `npm install`/`npm run dev` will fail or warn-and-break. Install Node 22 from NodeSource/nvm; or skip the frontend entirely (§10).

### 1.3 Minimal working install on noble

```bash
sudo apt-get update
sudo apt-get -y install git openssl python3 python3-pip python3-venv \
                        nmap mingw-w64 clang lld curl ca-certificates fuser
# Node 22 (vite 7 requirement) — NodeSource or nvm
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get -y install nodejs
# NetExec (no Ubuntu package)
sudo pip install --break-system-packages git+https://github.com/Pennyw0rth/NetExec   # or pipx
# Sliver (no Ubuntu package) — installs to /usr/local/bin, no systemd unit
curl https://sliver.sh/install | sudo bash

git clone http://10.0.0.132:3000/hnasher1/c3rberus.git && cd c3rberus
cp .env .env.bak                       # committed .env — rotate everything in §1.4
python3 -m venv venv && venv/bin/pip install -r requirements.txt
(cd react && npm install)              # optional; see §10
mkdir -p db logs
# Sliver server + unit
sudo tee /etc/systemd/system/sliver.service <<'UNIT'
[Unit]
Description=Sliver C2 server
After=network-online.target
[Service]
Type=simple
ExecStart=/usr/local/bin/sliver-server daemon
Restart=on-failure
User=root
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now sliver
venv/bin/sliver-server operator --name c3rberus --lhost localhost --permissions all \
      --save $PWD/sliver_operator.cfg   # regenerate: the committed one is a leak (§1.4)
scripts/run.sh                          # or run the bare uvicorn line (§3.1)
```

`scripts/run.sh` and `scripts/clean_env.sh` use `fuser` (`run.sh:80-81`, `stop.sh:44-45`) → package `psmisc`; and `clean_env.sh` step 5 **reboots every inventory VM** by default (`clean_env.sh:20-22`, skip with `--no-reboot-vms`) — never run it against a live range by accident.

### 1.4 Secrets actually committed to git — rotate before first boot

* `.env` (54 active keys) — includes `SCRIPT_HMAC_SECRET`, `JWT_SECRET_KEY`, `SCRIPT_ENCRYPTION_SALT`, `RED_SCORING_PASSWORD` (empty by default). Rotating `JWT_SECRET_KEY` invalidates all issued tokens; rotating `SCRIPT_HMAC_SECRET` invalidates in-flight script tokens only.
* `sliver_operator.cfg` — a complete JSON operator config (`{"operator","token","lhost","lport":31337,"ca_certificate"}`). Anyone with the repo can drive that C2 until you regenerate it.
* `certs/cert.pem` + `certs/key.pem` — committed TLS pair, subject `CN=SQORECARD`, valid 2025-09-24 → 2026-09-24. Because `setup.sh:44` only generates certs when `certs/cert.pem` is **absent**, `scripts/setup.sh` will happily serve the repo's private key. `rm -f certs/cert.pem certs/key.pem` first (or overwrite with your own). The same pair is reused by the backend (§3.1) and the script listener (§4.5).
* `config/cde26_inventory.yml` — real per-box plaintext passwords plus `red_ops_accounts` passwords. `config/inventory.yml` carries the shipped default lab admin creds.
* `fastapi_backend/database/db_init.py:628-641` seeds `admin` with a **hardcoded** password and `:~606` seeds `blueteam<N>` accounts with a deterministic pattern. Both must be changed via `POST /admin/change_password` (`routes/admin.py:232`) before the environment is reachable by anyone.

### 1.5 Runtime dirs / files created

`db/database.db` (`core/config.py:47`, but the engine actually uses `<repo>/db/database.db` resolved from the module path — `database/db_writer.py:11-17`), `logs/`, `certs/`, `venv/`, `implants/` (`config.py:524`), `modules/` (`config.py:536`), `nxc_workspace/` (`config.py:81`, `NXC_PATH`), `~/.sliver/` (server-side state, gRPC `:31337`).

---

## 2. Inventory schema (`config/inventory.yml`) + `c2_settings` semantics

Parser: `fastapi_backend/core/inventory_manager.py`. Path comes from `INVENTORY_FILE` (`config.py`, default `config/inventory.yml`), **force-defaulted** by `scripts/run.sh:20-27` — `run.sh` sources `.env` with `set -a` and then overwrites `INVENTORY_FILE` if it is unset, so pick the inventory by exporting it in `.env` (or in the shell before `run.sh`).

**Top-level keys observed across all shipped inventories** (`config/inventory.yml`, `cde26_inventory.yml`, `test_inventory*.yml`, `live_fire_inventory.yml`, `testing_inventory.yml`): `c2_settings`, `scoring_account`, `red_ops_accounts`, `netbios_pattern`, `teams[]`.

```yaml
c2_settings:                       # inventory_manager.py:227-238 ; applied at :342-378
  script_server_url: https://<C2_LAN_IP>:4000
  sliver_host: <C2_LAN_IP>
  c2_ip:     <C2_LAN_IP>
scoring_account:                   # :240-246
  username: red_scoring            # password is NEVER here — read from RED_SCORING_PASSWORD (.env)
  domain: ""
  enabled: true
red_ops_accounts:                  # optional, :251-258 (RedOpsAccount dataclass :83-112)
  - { username: redteam, password: <pw>, domain: true, enabled: true }
  - { username: svc_dfsr, password: <pw>, foreign_domain: <FQDN> }
netbios_pattern: "TEAM{team_num:02d}"    # consumed by promote_domain_controllers.py
teams:
  - team_num: 1
    name: Team 1
    domain: team1.example          # optional team-level realm (§ _infer_default_domain :140-156)
    boxes:
      - name: c3-win25-1-1
        ip: 192.168.1.10
        os: windows                # windows | linux | firewall   ← drives everything (§8)
        username: .\Administrator
        password: <pw>
        port: 5985                 # winrm; 22 for linux/firewall
        domain_username: "TEAM1\Administrator"
        domain_password: <pw>
        is_domain_joined: true
        is_domain_controller: true
        wan_ip: 10.50.50.1         # firewall only — outbound/dial-home address
        fetch_source_ip: 10.50.50.1  # src IP used to fetch scripts
      - { name: c3-ubuntu-1-1, ip: …, os: linux, username: root, password: …, port: 22 }
      - { name: c3-fw-1,       ip: …, os: firewall, username: admin, password: …, port: 22 }
```

Key semantics worth knowing:

* **`c2_settings` precedence** (`inventory_manager.py:342-378`): an explicit **process env** value for `SCRIPT_SERVER_URL`/`SLIVER_HOST`/`C2_IP` always beats the YAML; the YAML only fills what the env left unset. It is applied *before* `resolve_c2_ip()`, whose "fill only if unset" guard then sees the YAML value and skips auto-derivation.
* **`C2_IP` auto-derivation is a known trap** (`lifespan_helpers.py:352-376`): if unset, it binds a UDP socket to `8.8.8.8:80` and takes the local address of the **internet default route**, logging a WARN. On a multi-homed scorer that bakes an unreachable `c2_url` into every implant. Always set `C2_IP` explicitly (`wiki/sliver-setup.md:122-134`).
* **`script_server_url` derivation** (`config.py:760-773`): if unset *and* `C2_IP` set → `https://<C2_IP>:3000`, i.e. it silently points boxes at the **backend** port rather than the dedicated **4000** script listener. Every shipped inventory uses `:4000`. Set it explicitly.
* **`os: firewall` is a first-class value** meaning "FreeBSD/pfSense": protocol = SSH (anything not "windows" → `ssh`, `netexec/wrapper.py:2047-2056`), Sliver `goos = freebsd` (`core/deploy_all.py:57-58`), IOC set = `iocs/definitions/firewall_*.py`, naming pools keyed `freebsd` (`config/naming_pools.yml`).
* **Domain/realm inference** precedence (`inventory_manager.py:140-156`): box `domain:` → UPN suffix of `domain_username` → team-level `domain:` → legacy `team<N>.c3`.
* `scoring_account` is **declaration only** — the password lives in `RED_SCORING_PASSWORD`; the account must already exist on the boxes (provision it with `scripts/infra/create_red_scoring.py`, §2.1). `red_ops_accounts` passwords, by contrast, are stored plaintext in YAML and are seeded `is_infrastructure=False`, so they are never used by the IOC pipeline (`inventory_manager.py:83-112`, `orchestrator.py:605-626`).
* **YAML quoting gotcha** (`wiki/inventory-yaml.md:319-325`): a double-quoted `"TEAM1\Administrator"` leaves the backslash — use single quotes or `.\`-style raw values consistently.

### 2.1 The one-time box provisioning scripts (`scripts/infra/`)

| Script | What it does | Cite |
|---|---|---|
| `create_red_scoring.py` | Creates the scoring account **on the boxes** using the inventory admin/root creds. Linux: `useradd -o -u 0 -g 0 -M -d /root -s /bin/bash` (root-uid reuse); pfSense: `pw useradd <acct> -u 0 -o -g 0 -d /root -s /bin/sh`. Flags `--team N…`, `--dry-run`, `--verify-only`. | `:94`, `:219-245`, `:349`, `:294-297` |
| `promote_domain_controllers.py` | Windows AD build-out: phases `sysprep,rename,promote,forwarder,dns,join`; win1 → team DC, win2 → member. Needs WinRM `:5985`. DC role resolution: explicit `is_domain_controller: true` > name ending `-1` > first Windows box. | `:143-158`, `:609-614`, `:1082` |
| `join_linux_domain.py` | Linux → AD: `prereqs → dns → join → verify`. No pfSense/FreeBSD support (explicitly unimplemented). | `wiki/pre-competition.md:110-131` |
| `configure_networking.py` / `set_hostnames.py` | Static IPs (Linux + pfSense) and hostname = inventory `name`. Order: hostnames → networking → DC promote. | `wiki/pre-competition.md:133-144` |
| `fix_dc_clocks.py` | NTP-offset check/correction on all DCs (>270 s drift → `Set-Date`). Run after every DC reboot, before Kerberos work. | `:146-161` |
| `compat_setup.py` | Distro baseline prep + fingerprinting (`--fingerprint` default; `--setup`, `--install-tools`). | `:1-35` |
| `configure_scoring_nat.py` | NAT rules so boxes can reach the scorer. | `:54` |

`scripts/diagnostic/check_access.py` is the go/no-go gate: dials Windows via SMB (per-team domain creds), Linux via SSH, firewall via its **`wan_ip`** when set (`:36`), collects `os == "firewall"` boxes (`:142`), exit 0 only if everything is green. `wiki/pre-competition.md:187-203`.

---

## 3. Backend boot + scheduler control

### 3.1 Boot sequence (authoritative order)

`fastapi_backend/main.py` lifespan (`:104-166`):

1. asyncio debug / loop-heartbeat toggles (`C3R_LOOP_HEARTBEAT=1`, slow-callback threshold)
2. executor sizing — `C3R_ASYNCIO_EXECUTOR_MAX_WORKERS=64`
3. `init_runtime_dirs()` (`lifespan_helpers.py:57` creates `NXC_PATH` etc.)
4. `write_actor.start()` — single serialized SQLite writer thread
5. `CompetitionOrchestrator()` + `await orchestrator.initialize()` (`core/orchestrator.py`)
6. `truncate_volatile_tables()` — **deletes** `AlertReads`, `Alerts`, `ScheduledTask`, `OperatorTask` rows on every boot (`lifespan_helpers.py:146-180`)
7. `init_implant_subsystem()` — migrate `./implants/<uuid>/` → `implant_builds` rows, rescan `modules/`, load naming pools, load `config/services.yml` (`lifespan_helpers.py:287-307`)
8. `init_nxc()` unless `DEV_MODE` (`:310-349`)
9. `resolve_c2_ip()` (`:352-376`)
10. `sliver_manager.startup()` + `purge_stale_startup_beacons()` unless `DEV_MODE`
11. `prune_stale_sa_results(24h)`, `check_nmap(app)`, `init_hub_registries(app)`, `log_script_server_info()`
12. `register_background_loops()` (`:777-830`) → hub cleanup, metrics, wrapper sweep, staged-upload sweep, **script listener on :4000**, SA collection (NXC + Sliver), beacon watcher, nmap health, alert cleanup, beacon-task poller, `scheduler_loop` (playbook/ScheduledTask), pivot keepalive, cred harvesters
13. `arm_write_guard()`, `yield`

Inside `orchestrator.initialize()` (steps 1-6): DB init + migrations, `InventoryManager` load → `Boxes` upsert + prune stale teams/boxes (`db_init.py:561`, `:580`) + `initialize_teams`/`initialize_accounts`, IOC loader glob of `iocs/definitions/*.py` (`core/ioc_definition.py`), `_seed_scoring_creds()` (`:519-604`), `_seed_red_ops_creds()` (`:605`), `_seed_default_cred_iocs()` (`:719`), `_seed_pre_deployed_iocs()` (`:757`), `restore_competition_start_at()`, `_read_persisted_status()` → `_set_status(NOT_STARTED)`, `_restore_schedule_jobs_on_boot()`, then `_restore_competition_status_on_boot(stored)`, plus the credential loops (`:276-295`).

The actual server command (`scripts/run.sh:124-133`):

```bash
venv/bin/uvicorn fastapi_backend.main:app --host 0.0.0.0 --port 3000 \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem \
  --backlog 4096 --limit-concurrency 500 --timeout-keep-alive 15
```
`ulimit -n 8192` first (`run.sh:117`); logs are timestamped per run with 7-day prune (`run.sh:67-74`); readiness = poll `https://localhost:3000/docs`, and on timeout it **kills the frontend and exits 1** (`run.sh:145-159`). Reload is off unless `C3R_RELOAD=1`/`--reload` (`run.sh:101-112`).

### 3.2 State machine

`core/competition_state.py:10-35` — `NOT_STARTED, INITIALIZING, PREFLIGHT_ON, PREFLIGHT_ARMED, RUNNING, PAUSED, STOPPED, ERROR` with an explicit `VALID_TRANSITIONS` table. Checks only fire when `can_run_checks()` is true, i.e. `RUNNING | INITIALIZING | PREFLIGHT_ON | PREFLIGHT_ARMED` (`:109-116`).

### 3.3 Scheduler control

`core/scheduler.py` — `CheckScheduler(check_interval_minutes=5)` (an asyncio task, **not** APScheduler despite what the docstring and `wiki/architecture.md` say). `start()` runs `_run_check()` **immediately** then loops, re-reading `config.CHECK_INTERVAL_MINUTES` each cycle, so `PATCH /admin/settings` changes take effect on the next cycle without a restart. `stop()`, `pause()`, `resume()` (resume fires immediately), `get_status()` → `{running, paused, interval, last_run, next_run}`.

Control surface (`routes/admin.py`, all admin-gated, `check_admin` at `:114`):

| Action | Endpoint | Line |
|---|---|---|
| Start competition | `POST /admin/start_comp` | `:713` |
| Stop | `POST /admin/stop_comp` | `:1084` |
| Pause | `POST /admin/pause_comp` | `:1106` |
| Resume | `POST /admin/unpause_comp` | `:1128` |
| Preflight on / stop | `POST /admin/preflight/start`, `/preflight/stop` | `:763`, `:777` |
| Preflight arm / disarm | `POST /admin/preflight/arm` `{start_time:"<ISO>"}`, `/preflight/disarm` | `:791`, `:834` |
| Preflight results | `GET /admin/preflight/results` | `:997` |
| Manual check sweep | `POST /admin/run_checks` | `:667` |
| Schedule edits | `PATCH /admin/schedule` | `:872` |
| Health | `GET /admin/status` | `:1150` |
| Runtime settings | `GET/PATCH /admin/settings` | `:1273`, `:1282` |
| Rollback / purge | `POST /admin/rollback`, `/preflight/purge` | `:983`, `:962` |

**Answer to "does the checker autostart on boot?"** No, for a fresh DB: `_read_persisted_status()` → `_set_status(NOT_STARTED)` and nothing calls `scheduler.start()`. **But** if the persisted status was `RUNNING`/`PAUSED`, `_restore_competition_status_on_boot()` (`orchestrator.py:1414-1478`) calls `start_competition()` automatically — i.e. **restarting the backend mid-competition silently resumes scoring** and emits a `competition_resumed_after_restart` alert; if the scheduled end already passed it restores as `STOPPED` with reason `scheduled_end_passed` (`wiki/troubleshooting.md`, "Component restart procedure").

Also: the **IOC check sweep is state-gated, but the boot-time background loops are not.** With defaults, `loop_sa_collection` and `loop_nmap_health` **fire an immediate first cycle at startup** (`lifespan_helpers.py:802-815`, `:490-496`) and `_scoring_cred_probe_loop` / `_cred_revalidation_loop` start inside `initialize()` (`orchestrator.py:276-295`). So booting the backend against a populated inventory **touches every box before T0** even though no IOC is scored. To hold the environment pristine until you're ready:

```
SA_COLLECTION_ENABLED=false
NMAP_HEALTH_ENABLED=false
CRED_REVALIDATION_ENABLED=false
PIVOT_KEEPALIVE_ENABLED=false
CRED_HARVEST_ENABLED=false        # already false by default (config.py:421)
```
(Then flip `SA_COLLECTION_ENABLED`/`NMAP_HEALTH_ENABLED` back on at T0 via `PATCH /admin/settings` — both are in `_MUTABLE_SETTINGS`.)

---

## 4. Operator HTTP API

### 4.1 Auth

* `POST /login` `{username, password}` → `{"token", "user_id", "username", "admin", "is_blue_team", "blueteam_num"}` (`routes/login.py:30-61`). ⚠️ `wiki/api-reference.md:18` documents the field as `access_token` — **wrong**; the JSON key is `token`.
* Bearer only: `Authorization: Bearer <jwt>` (`utils/auth.py`, `OAuth2PasswordBearer(tokenUrl="login")`). JWT HS256, payload `{"sub": user_id}`, **no expiry** (`config.py:688-693`). No cookies, no CSRF → curl is first-class.
* Roles: `is_admin`, `is_blue_team`, `blue_team_num`. Gates: `check_admin` → 403 (`routes/admin.py:114`), `require_non_blue` → admins+red/white/black only (`routes/c2.py:226`), `check_non_blue_team` for IOC definitions (`routes/iocs.py:84`). Blue callers of `/details` see only their own team with obscured IOC labels.
* SSE/WebSocket can't set headers → they take `?token=<jwt>` (`routes/tasks.py:96`, `routes/c2.py:7206`, `validate_token_param` at `c2.py:232`).
**Unauthenticated on purpose:** `GET /scoreboard`, `GET /scoreboard/history`, `GET /boxes` (`routes/scoreboard.py:41/:130/:147`), plus `/docs`, `/redoc`, `/wiki/*`. Treat the host as LAN-trust-only.

Scripting pattern:

```bash
API=https://red01.lan:3000
TOK=$(curl -sk -X POST $API/login -d 'username=admin&password=<pw>' | jq -r .token)
H="Authorization: Bearer $TOK"
```

### 4.2 IOC catalog reads

`routes/iocs.py` (prefix `/iocs`): `GET /iocs/`, `GET /iocs/by-os/{os}`, `GET /iocs/{ioc_name}`, `GET /iocs/stats/summary` (`total_iocs`). Each entry exposes `has_deploy_script`, `has_effect_deploy_script`, `is_sliver_check` (`wiki/architecture.md` routes list).

### 4.3 IOC deploy / check

`routes/ioc_deploy.py` (prefix **`/c2/iocs`**):

| Endpoint | Body / notes | Line |
|---|---|---|
| `POST /c2/iocs/deploy` | `{ioc_name, box_ips[], persistence_mechanism, implant_identifier, deployed_by, timestomp:false, deploy_params:null}` | `:417`, `:532` |
| `POST /c2/iocs/deploy-and-register` | as above + `cred_id`; creates/updates the `DeployedIOCs` row (the row scoring keys off) | `:436`, `:655` |
| `GET /c2/iocs/deployed?box_ip=&status=` | per row: `remediated`, `last_effect_status`, `last_persistence_status`, `deploy_params`, `deploy_error`, `deploy_task_id`, `deploy_attempts`, `highlight_patterns` | `:1288` |
| `GET` / `DELETE /c2/iocs/deployed/{id}` | | `:1388`, `:1442` |
| `POST /c2/iocs/redeploy` | re-run deploy for an existing row | `:1496` |
| `POST /c2/iocs/deregister-all` | | `:1582` |
| `GET /c2/iocs/deployment-stats` | | `:1665` |

Admin-level bulk (`routes/admin.py`): `POST /admin/deploy_iocs` (legacy, `:259`), `POST /admin/deploy-all-iocs` (`:305`, body `DeployAllRequest` `:78` = `{include_implants:true, skip_active:true, confirm:false, team_nums:null, ioc_names:null}`; `202 {task_id}`; `409 another_deploy_all_running`; `409` if `RUNNING` without `confirm:true`), `POST /admin/build-all-iocs` (`:367`, `BuildAllRequest` `:100`), `POST /admin/build-check` (`:405`), `POST /admin/deploy_iocs_selective` (`:528`), `POST /admin/redeploy_iocs` (`:558`), `POST /admin/remediate_iocs` (`:622`), `POST /admin/scoring-cred-preflight` (`:454`).

`/admin/status` payload keys: `database`, `inventory`, `iocs{loaded,by_os}`, `scheduler`, `executor`, `check_cycles`, `sa_collection`, `script_encryption`, `script_server`, `playbook`, `competition_restart`, `scoring_creds`, `configuration` (`orchestrator.py:1854-1960`).

Check results: `GET /details?team_num=N` (`routes/details.py:19`) returns `team_num, box_ip, ioc_name, difficulty, status, effect_status, persistence_status, error, points` from the `team_latest_ioc_details` view. Live progress via `GET /c2/events` SSE (`c2.py:1387`) and `GET /c2/tasks[/{id}[/stream]]` (`routes/tasks.py:96-202`).

### 4.4 Sliver implants / beacons

`routes/c2.py` (prefix `/c2`, `require_non_blue` at `:226`). Highlights:

* `POST /c2/implants/generate` → `ImplantGenerateRequest` (`:5539`): `os` ∈ `windows|linux|freebsd`, `arch="amd64"`, `format="exe"`, **`is_beacon` required**, `beacon_interval=60`, **`c2_url` required**, `deploy_targets[]`, `pivot_team_nums[]`, `tcp_pivot_port`, `persistence_name`, `persistence_tier_group`, `apply_immutable`, `immutable_scope`, `auto_register_ioc_name`, `timestomp`, `backup_copy_enabled`, `use_scoring_cred=false`, `storage_mode` ∈ `file|registry|pe_section|xattr|elf_section|extattr|self_inject|self_inject_persist`, `inject_persist_type`. Server-side default `c2_url` if omitted elsewhere is `mtls://<C2_IP||SLIVER_HOST>:8888` (`:2063`).
* `GET /c2/implants/builds` (`:6440`), `GET /c2/implants/builds/{id}` (binary, `:6507`), `POST /c2/implants/{build_id}/deploy` (`:6596`, `{target_ips[], cred_id, username, password}`) — this one **409s with `{"code":"stale_sliver_config"}`** if the build's stem is gone from Sliver's live `implant_builds()` list (`wiki/subsystems.md:47`).
* Sessions: `GET /c2/sessions` `:1619`, `POST /c2/sessions/{id}/exec` `:1726` (`{command, args[]}`), upload/download `:1790/:1811`, `inject-shellcode` `:1859`, `DELETE /c2/sessions/{id}` `:2459`, `POST /c2/sessions/kill-all` `:2535`, WS shells `:6988` / `:7087`.
* Beacons: `GET /c2/beacons` `:2609`, `POST /c2/beacons/{id}/exec` `:2675`, beacon tasks `:2748`/`:2773`, `POST /c2/beacons/{id}/interactive` `:2800` (queues an `OpenSession` task, returns `{operator_task_id, state:"queued"}`), `DELETE /c2/beacons/{id}` `:2860`, `POST /c2/beacons/kill-all` `:2917`.
* Listeners: `GET /c2/listeners` `:6528`, `POST /c2/listeners/mtls` `:6551` (`{host:"0.0.0.0", port:8888, persistent:false}`).
* Bulk: `POST /c2/bulk/exec/async` `:2987`, `POST /c2/bulk/put-file/stage|async` `:1150`/`:1190`.
* Creds: `GET/POST /c2/creds` `:560`/`:500` (`CredentialCreate`, `models.py:274-298`: `username,password,box_ip,protocol(smb|ssh|winrm),is_default,cred_type(password|nthash|-ticket-…),nthash,domain,team_num`), delete `:598`/`:615`, `POST /c2/creds/test-all` `:1062`, `POST /c2/creds/{id}/test` `:1102`.
* Pivot: per-team TCP-pivot listener on `PIVOT_LISTENER_PORT_BASE + team_num` = **9901 for team 1** (`config.py:519`); base default `9898` (`:514`); `POST /admin/prewarm-pivots` (`admin.py:508`); `GET /c2/pivot-sessions`.
* Misc: `GET /c2/server-info` → `{c2_ip, display_timezone, dev_mode}` (`:323`), `GET /c2/boxes` `:487`, `GET /c2/alerts` `:1444`, `POST /c2/harvest/{box_ip}` `:645`, `POST /c2/forge-ticket` `:737`, `GET /c2/tickets` `:848`, SA `:6961`+, `WS /c2/sliver-console` `:7206`.

⚠️ `wiki/api-reference.md` is stale here: it lists `GET /c2/implants` (real: `/c2/implants/builds`), `/admin/operations` (real: `/admin/start_comp|stop_comp|pause_comp|unpause_comp|reset_data`), `/admin/users` (real: `/admin/get_users|add_user|remove_user|change_password`), `/admin/alerts` (real: `/c2/alerts`), and a 5-state machine "Init/Ready/Running/Paused/Shutdown" that no longer matches `competition_state.py:10-35`.

### 4.5 Script-delivery plumbing (why port 4000 exists)

A dedicated off-event-loop HTTPS listener serves IOC script bodies on `SCRIPT_LISTENER_PORT=4000` (`config.py:306-314`, started `lifespan_helpers.py:793-801`, bind failure is **fatal to startup**: "Set SCRIPT_LISTENER_PORT to a free port or SCRIPT_LISTENER_ENABLED=false"). URLs are HMAC-signed, single-scope tokens bound to (box IP, script path, expiry) with `SCRIPT_HMAC_SECRET` (`config.py` `:~250`); result callback endpoint `POST /scripts/wrapper/{token_id}/result` (`routes/scripts.py`). The main app also mounts `/scripts` as a fallback (`main.py:220-224`). Firewall egress is pinned with `iptables -N C3R_SCORING` / `-A C3R_SCORING -s <IP> -p tcp --dport 4000 -j ACCEPT` (`lifespan_helpers.py:837-858`).

---

## 5. Sliver server + operator setup for a new competition

1. **Install server** — `curl https://sliver.sh/install | sudo bash` (`wiki/sliver-setup.md:25`) → `/usr/local/bin/sliver-server`, `/usr/local/bin/sliver-client`. (`setup.sh:82/:89` assumes `/usr/bin/sliver-server` + a `sliver` unit — fix one or the other.)
2. **Run it as a daemon** with a systemd unit (`sliver_manager.py:545-560` shells out `systemctl start sliver` as its auto-recovery path, so the unit name must literally be `sliver.service`). gRPC binds `127.0.0.1:31337`; state in `/root/.sliver/`.
3. **Create the operator config** — `sliver-server operator --name c3rberus --lhost localhost --permissions all --save <repo>/sliver_operator.cfg`, then `chown $USER`. The backend reads this file (`config.py:489 SLIVER_CFG_PATH=./sliver_operator.cfg`; parsed with unknown fields stripped at `c2/sliver_manager.py:102-111`) — **this is a gRPC credential, not a CLI config**. Import the same `.cfg` into the desktop/CLI `sliver-client` for manual work.
4. **mTLS listener** — backend auto-ensures it: `jobs()` for a listener on `config.MTLS_LISTENER_PORT` else `start_mtls_listener(host="0.0.0.0", port=8888)` (`sliver_manager.py:684-723`). Manual equivalent: `sliver-client console <<< 'mtls -L 0.0.0.0 -l 8888'` (`setup.sh:97`).
5. **Enable in `.env`** — `SLIVER_ENABLED=true`. Default is **false** (`config.py:470`), and `startup()` returns immediately when false (`sliver_manager.py:505`), so with a stock `.env` nothing ever talks to Sliver. Missing `SLIVER_CFG_PATH` → warn + self-disable.
6. **Set the network identity** — `C2_IP=<the IP the boxes actually route back to>` (`config.py:520`), `MTLS_LISTENER_PORT=8888` (`:495`), `PIVOT_LISTENER_PORT_BASE=9900` (`:519`), `SCRIPT_SERVER_URL=https://<C2_IP>:4000`. These three may instead come from `c2_settings:` in the inventory.
7. **Implant generation** uses `sliver-py` gRPC (`requirements.txt` `sliver-py>=0.0.19`); the **`sliver-client` binary is only used for the web terminal PTY** (`c2/sliver_console.py:69 base=[config.SLIVER_CLIENT_BIN,"console"]`). `SLIVER_BIN` is effectively unused; `SLIVER_PORT` is a phantom key that no code reads (`wiki/configuration.md:291` admits it).
8. **Per-competition reset** — `scripts/reset_sliver.sh` (purges sessions/beacons, restarts the service, cleans implant artifacts; calls `clean_stale_implants.sh --days 0` at `:78`; edits `/root/.sliver/sliver.db` directly, so the service must be stopped first — `:35`). DB-only reset: `scripts/reset_db.sh`, or `POST /admin/reset_data` (`admin.py:1378`, 400 while running; deletes `IOCCheckResult`/`CheckInstance`/`DeployedIOCs`).
9. **Runtime-compiled `.so` implants** need a low-glibc builder: `docker/sliver-build/` runs `sliver-server` in Ubuntu 18.04 (glibc 2.27) via podman with `/root/.sliver` bind-mounted, so generated `.so` loads on CentOS 8 / Ubuntu 20.04 targets. `.env` key `EXTERNAL_BUILDER_SO_LINUX` points the `linux+shared` build at it (`c2.py:3120-3129`). Optional; skip if you have no such targets.
10. **FreeBSD/pfSense binaries**: `implant_src/*bsd*.c` compile with `clang --target=x86_64-unknown-freebsd15 -fuse-ld=lld`; the BSD "hide library" and `.so` tiers need a FreeBSD 15 sysroot under `scripts/build/fbsd15-sysroot/` (or `/tmp/fbsd15-sysroot`) — built by `scripts/build/provision_fbsd_dynamic_sysroot.sh`, and cleanly `[skip]`ed when absent (`scripts/build/build_payloads.sh:29-30,46-49`). Pre-compile with `scripts/build/build_payloads.sh` (`--force` to rebuild) — it explicitly notes the three Sliver beacons (`beacon_linux`, `msupdate.exe`, `beacon_freebsd`) still have to come from the Generation UI + `build_shellcode.sh`.

---

## 6. Programmatic lifecycle runbook

```bash
API=https://red01.lan:3000
TOK=$(curl -sk -X POST $API/login -d 'username=admin&password=<ADMIN_PW>' | jq -r .token)
H="Authorization: Bearer $TOK"; CT='Content-Type: application/json'

curl -sk -H "$H" $API/admin/status | jq '.components | {inventory, iocs, scheduler, sliver: .script_server}'
# 0. pre-flight: python scripts/diagnostic/check_access.py   (run out of band, not via API)
#    then validate the scoring identity on every box:
curl -sk -H "$H" -H "$CT" -X POST $API/admin/scoring-cred-preflight -d '{"team_nums":[1]}'   # 202 {task_id}
curl -sk -H "$H" $API/c2/tasks/<task_id> | jq '.progress_json, .metadata_json'                # reason: pass|auth-fail|insufficient-perms|unreachable|absent

# 1. build implant binaries (no box I/O)
curl -sk -H "$H" -H "$CT" -X POST $API/admin/build-all-iocs -d '{"include_implants":true,"force":false,"team_nums":[1]}'

# 2. plant the IOC catalog (allowed pre-competition; only RUNNING requires confirm)
curl -sk -H "$H" -H "$CT" -X POST $API/admin/deploy-all-iocs -d '{"include_implants":true,"skip_active":true,"team_nums":[1]}'
curl -sk -H "$H" $API/c2/iocs/deployed?box_ip=192.168.1.10 | jq '.[] | {ioc_name,last_effect_status,last_persistence_status,deploy_error}'

# 3. single IOC onto single box, registered for scoring
curl -sk -H "$H" -H "$CT" -X POST $API/c2/iocs/deploy-and-register \
  -d '{"ioc_name":"cron_backdoor","box_ips":["192.168.1.20"],"deployed_by":"admin","timestomp":true}'

# 4. start scoring NOW  (or arm it for a wall-clock T0)
curl -sk -H "$H" -H "$CT" -X POST $API/admin/start_comp
#    …or the scheduled route:
curl -sk -H "$H" -H "$CT" -X POST $API/admin/preflight/start -d '{}'
curl -sk -H "$H" -H "$CT" -X POST $API/admin/preflight/arm    -d '{"start_time":"2026-03-01T09:00:00"}'
curl -sk -H "$H" $API/admin/preflight/results | jq            # what the sweep found pre-T0

# 5. pause / resume / stop
curl -sk -H "$H" -X POST $API/admin/pause_comp ; curl -sk -H "$H" -X POST $API/admin/unpause_comp
curl -sk -H "$H" -X POST $API/admin/stop_comp           # clears playbook jobs, scheduler.stop(), stops workers

# 6. live reads
watch -n30 "curl -sk $API/scoreboard | jq '.teams[] | {team_num,total_score,score_pct,last_check_score}'"
curl -sk -H "$H" "$API/details?team_num=1" | jq '.[0:5]'
curl -sk -H "$H" $API/admin/status | jq '.components.scheduler'   # {running,paused,interval,last_run,next_run}
curl -sk -N "$API/c2/tasks/stream?token=$TOK"                      # SSE task feed
curl -sk -N "$API/c2/events"                                      # SSE Sliver events + alerts
# C2 health:
curl -sk -H "$H" $API/c2/sliver/status ; curl -sk -H "$H" $API/c2/sessions ; curl -sk -H "$H" $API/c2/beacons
# runtime knob changes (allowlisted keys only, 422 otherwise — admin.py:1282 + _MUTABLE_SETTINGS):
curl -sk -H "$H" -H "$CT" -X PATCH $API/admin/settings -d '{"CHECK_INTERVAL_MINUTES":2}'
```

Manual bypass vs. scheduler: `POST /admin/preflight/arm` writes `CompetitionConfig.armed_start_time` and registers an `armed_start` asyncio job (`admin.py:791` + `orchestrator.py:_restore_schedule_jobs_on_boot`, which also re-arms `scheduled_end` / `scheduled_pause` / `scheduled_resume`). `POST /admin/start_comp` is the immediate, non-scheduled start. `PATCH /admin/schedule` (`:872`) edits them; `POST /admin/playbook/reload` (`:1328`) re-reads `config/playbook.yml` without a restart (`playbook.example.yml:1-34` shows the mapping form: `start_time` absolute, `end_time`/`pause_at`/`resume_at` as `T+offset`, `entries[]` fanning out to all boxes matching `os`).

---

## 7. Artifacts for the after-action report

Everything is on the scorer host:

| Artifact | Path | Notes |
|---|---|---|
| SQLite DB | `db/database.db` (+ `-wal`, `-shm`) | resolved from module location, `db_writer.py:11-17`; `DATABASE_URL` override at `config.py:47` |
| — check ledger | `check_instance`, `ioc_check_result`, `check_failure_diagnostics` | one `check_instance` per sweep |
| — state | `deployed_iocs` (`deploy_params`, `deploy_error`, `deploy_task_id`, `deploy_attempts`), `boxes`, `credentials`, `blue_teams`, `users`, `implant_builds`, `pivot_sessions`, `portfwd_mappings`, `pending_kills`, `sa_results`, `alerts` + `alert_reads`, `operator_tasks`, `scheduled_tasks`, `scheduled_competition_events`, `competition_config`, `modules`, `hiding_library_configs`, `staged_uploads` | `database/models.py:40-728` |
| — **SQL views (the scoring truth)** | `blue_teams_scoreboard`, `team_latest_ioc_details`, `score_history`, `ioc_check_result_with_points`, `check_instance_with_score` | `db_init.create_scoring_views()`; `wiki/db-schema.md:200-208`; partial/bonus math `IOC_PARTIAL_SCORE_PCT=40`, `IOC_COMPLETION_BONUS_PCT=20` (`config.py:352-353`) |
| Logs | `logs/fastapi_backend_<ts>.log`, `logs/uvicorn_<ts>.log`, `logs/react_<ts>.log`, `logs/pytest_<ts>.log`, `logs/resource_usage_<ts>.tsv`, `logs/xdist_workers/session_lifecycle.log` | `config.py:744`, `run.sh:67-74` |
| Binaries | `implants/<uuid>/` (`IMPLANT_STORE_DIR`, `config.py:524`), `build/shim_cache/`, `build/artifact_manifest.json` | migrated into `implant_builds` at boot |
| C2 state | `/root/.sliver/{sliver.db,builds,slivers}/` | operator-token-bearing |
| NXC state | `nxc_workspace/` (`NXC_PATH`, `config.py:81`), `nxc_workspace/ccache/<cred_id>.ccache` (`c2.py:916`), `nxc_workspace/uploads/…` (`c2.py:1168`) | |
| Post-hoc box evidence | `scripts/diagnostic/pull_box_logs.py` | mentioned in `wiki/troubleshooting.md` audit note (2026-08-02) |

**No CSV/export endpoint exists.** For reporting: `sqlite3 db/database.db "SELECT * FROM score_history"` / `team_latest_ioc_details`, or loop `GET /details?team_num=N` + `GET /scoreboard/history`. Snapshot `db/database.db` (and stop the backend or use `.backup`) before anything else — `truncate_volatile_tables()` wipes `alerts`/`operator_tasks`/`scheduled_tasks` on the next boot, so those tables are **per-session only** and must be harvested before a restart.

---

## 8. Which boxes get which IOCs — the actual applicability rule

**One rule, no intelligence: exact lowercase string equality `ioc.os == box.os`.**

* `core/deploy_all.py:86-97` builds `{ioc_os: [box_ip]}` purely from the inventory `Box.os` field; `:554`/`:635` apply an IOC only to boxes whose `os` matches. `iocs/base.py` requires `os ∈ {windows,linux,firewall}`; `core/ioc_definition.py` produces `os_ioc_mapping` keyed those three.
* **There is no OS detection anywhere** — no nmap-based classification feeding `Box.os`. `os:` must be hand-written and exact; `"Windows"`, `"pfsense"`, `"freebsd"` in the inventory silently receive **zero** IOCs.
* `firewall` ⇒ Sliver `goos=freebsd` (`deploy_all.py:57-58`), SSH delivery, `freebsd` naming/dir pools (`config/naming_pools.yml`), and the `firewall_*` IOC set.
* Difficulty is a **per-IOC constant** (1-4), not a selection knob: it maps to points via `DIFFICULTY_{EASY,MEDIUM,HARD}_POINTS` = 5/10/20/40 (`base.py` `points` property, `config.py:346-349`; `.env` exposes all four names). Difficulty then drives *where* and *what it is called*: each pool directory carries a tier and "a higher difficulty draws only from higher-or-equal tiers" (`c2/deploy_paths.py:7-10,33-56`; `c2/deploy_params.py:94-149` `pick_unused_deploy_path(os_key(box_os), difficulty=diff, exclude=…)`; tier data in `config/naming_pools.yml` `dir_pools.{windows,linux,freebsd}` with tiers 1-3).
* Catalog: 86 definition modules → **32 windows / 31 linux / 24 firewall** (one module defines two), difficulty spread 20×D1, 28×D2, 23×D3, 16×D4. Script counts: 89 deploy / 90 remediate / 86 check under `iocs/scripts/`.
* `Deploy All` set = every IOC with `deploy_all_default=True` (default; `base.py:236`) **and** `disabled=False` (`base.py:246`; `disabled` is an unconditional block, even `ioc_names:[…]` can't force it — `core/deploy_all.py:145-173`). Opt-in high-blast-radius IOCs (`linux_pam_any_password`, `firewall_pam_any_password`, `windows_rid_hijack`, `windows_adminsdholder`, all resilient/self-inject/embed variants) need explicit `ioc_names` or the Deployment page (`wiki/running-competition.md:107-127`). Inject-type IOCs are always skipped (`reason: inject_ioc`). `requires_dc=True` (`base.py:~250`, 4 IOCs) fans out only to `Boxes.is_domain_controller`. `pre_deployed=True` (3 IOCs) means C3rberus only *scores* an artefact planted out of band by e.g. `scripts/infra/rogue_dc_admin_persist.py` — a virtual `DeployedIOCs` row is seeded at startup and protected from deregistration.
* **Box names are cosmetic to the engine** — they matter only (a) to the AD helper's `-1`/`-2` DC-vs-member convention (`promote_domain_controllers.py:609-614`), and (b) as the Kerberos SPN `host/<name>.<realm>` (`wiki/inventory-yaml.md`). Set them anyway so hostname/Kerberos paths line up.

---

## 9. Gotchas for an offline-ish host named red01

1. **`nxc` and `sliver` are not installable by apt on Ubuntu.** `setup.sh:29-41` will `apt-get install netexec sliver` and *fail* (or silently no-op if it only checks the exit code of the aggregate command). Bootstrap both from source/upstream URLs (§1.2). NetExec needs its own Python deps; Sliver is a single static binary download — cache `sliver-server`/`sliver-client` and a NetExec wheel on the runner.
2. **Node 18 from noble breaks the frontend** (vite 7 wants ≥20.19/≥22.12). Install Node 22 or don't build the UI.
3. **No network → Playwright hard-fails.** `setup.sh:73-76` runs `npx playwright install chromium`; `run_tests.sh:190-211` re-runs it with a 2-attempt retry then continues. Use `scripts/setup.sh --dev` or pre-seed `~/.cache/ms-playwright`.
4. **Committed TLS key is reused, not regenerated**, because `setup.sh:44` only writes certs when the file is missing. Delete `certs/*` first. It expires 2026-09-24.
5. **`certs/key.pem` and `cert.pem` must exist for the backend to start at all** (`run.sh:124-133` passes `--ssl-*`), and the same pair is reused by the `:4000` script listener (`lifespan_helpers.py:747-751`).
6. **A `:4000` bind failure kills startup** (`lifespan_helpers.py:753-764`) — reserve that port, or set `SCRIPT_LISTENER_ENABLED=false` (then boxes can't fetch script bodies and script-based IOCs fail wholesale).
7. **`C2_IP` auto-derivation picks the wrong NIC on a multi-homed host** (`lifespan_helpers.py:352-376`). Set `C2_IP=<red01 LAN IP>` explicitly; verify via `GET /c2/server-info`.
8. **`SCRIPT_SERVER_URL` defaults to `https://<C2_IP>:3000`** (`config.py:760-773`) — inconsistent with the 4000 listener that everything else assumes. Set it explicitly.
9. **Boot touches the boxes** — SA collection + nmap health fire immediately, cred probes/revalidation start in `initialize()`. Disable the five flags in §3.3 to keep pre-T0 silent.
10. **`scripts/clean_env.sh` reboots every inventory VM** (step 5) and then runs `remediate_persistent_iocs.py` on any box still ≥20% CPU (step 6). It is called by `restart.sh`. Never run it during a competition. It also always exits 0 — failures are invisible to callers.
11. **Restart mid-competition = scoring silently resumes** (`orchestrator.py:1414-1478`). If you restart for config reasons, either stop first or expect results to keep accumulating.
12. **`truncate_volatile_tables()` on every boot** deletes all `alerts`, `operator_tasks`, `scheduled_tasks` — harvest them before restarting (§7).
13. **`sliver-py` version drift**: the operator config is JSON-filtered against a known-field list because newer servers add fields sliver-py can't parse (`sliver_manager.py:102-111`). Pin server + `sliver-py` to compatible versions; a too-new server breaks connect, not just typing.
14. **`SLIVER_ENABLED` defaults false** (`config.py:470`) → C2 features silently inert with a stock `.env`.
15. **`.env` keys that do nothing:** `COMPETITION_START_TIME`, `SLIVER_PORT`, `IOC_DEFINITIONS_DIR`, `VENV_DIR`, `C3R_SLIVER_TRACE*` (0 Python references each; confirmed by grep). Don't debug through them.
16. **The shipped `config/inventory.yml` is a 10-team lab** (`c3-fw-<t>`, `c3-win25-<t>-1/2`, `c3-ubuntu-<t>-1/2`, 10×5 boxes) pointing at `192.168.1.161`. Replace it wholesale for a 1-team range.
17. **`/scoreboard`, `/scoreboard/history`, `/boxes` are unauthenticated** (`scoreboard.py:41/130/147`) — don't expose red01 beyond the competition VLAN.
18. **pfSense domain/Kerberos join is unimplemented** (`wiki/pre-competition.md:129-131`); BSD hiding-library recovery needs offline `extattr` cleanup (`wiki/troubleshooting.md` audit note).
19. **Default seeded logins** (`db_init.py:606-641`, `README.md:28`) are public knowledge — change `admin` immediately via `/admin/change_password`.
20. **`fuser` (psmisc) required** by `run.sh:80-81` / `stop.sh:44-45`; `stop.sh`'s PID-file logic is commented out as broken (`:38-39`), so stopping = "kill whatever owns the port".

---

## 10. Is the frontend required?

**No.** Nothing in the backend serves or needs it: there is no `StaticFiles` mount in `main.py`, and every UI action has an HTTP/WS equivalent (§4). The UI is a Vite dev server on `:5173` with `server.proxy['/api'] → BACKEND_URL ?? https://localhost:3000`, HTTPS from the same `certs/`, `host: true`, `hmr: false`, `usePolling: true` (`react/vite.config.ts`). `run.sh` starts it (`:137`) but a failed frontend readiness check still exits 1 (`:145-159`), so for a lean scorer skip `run.sh` and launch uvicorn directly:

```bash
set -a; . ./.env; set +a
ulimit -n 8192
exec venv/bin/uvicorn fastapi_backend.main:app --host 0.0.0.0 --port 3000 \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem \
  --backlog 4096 --limit-concurrency 500 --timeout-keep-alive 15
```
You lose `/admin`, `/c2/*` operator pages and the wiki UI (the wiki is served by the backend at `/wiki/*` regardless). Everything else — deploy, check, start/stop, scoreboard — is reachable by API. Blue teams, however, need a UI: the scoreboard/details endpoints are written for the React front end.

---

## 11. Docs-vs-code disagreements (flag list)

| # | Doc says | Code says |
|---|---|---|
| 1 | `wiki/api-reference.md:18` login → `{access_token}` | `{token,…}` — `routes/login.py:30-61` |
| 2 | `api-reference.md:136` `POST /admin/operations` (start/stop/pause/resume/reset); `:141 /admin/alerts`; `:142 /admin/users`; `:37 GET /c2/implants` | `/admin/start_comp\|stop_comp\|pause_comp\|unpause_comp\|reset_data`; `/c2/alerts`; `/admin/get_users\|add_user\|remove_user`; `/c2/implants/builds` — `admin.py:713,1084,1106,1128,1378`, `c2.py:1444,6440` |
| 3 | `api-reference.md:108` states 5-state machine Init/Ready/Running/Paused/Shutdown; `wiki/architecture.md` §Backend repeats it | 8 states incl. `PREFLIGHT_ON/ARMED/ERROR` — `core/competition_state.py:10-35` |
| 4 | `wiki/pre-competition.md:15` "Python 3.14, Node 22+"; `CLAUDE.md:139` 3.14 | `ruff.toml:2 py313`; noble ships 3.12 / Node 18 (frontend breaks) |
| 5 | `setup.sh:82,89,97` `/usr/bin/sliver-server` + `sliver.service` | `wiki/sliver-setup.md:25` upstream installer → `/usr/local/bin`, **no unit**. One of the two must be fixed |
| 6 | `setup.sh:29-41` `apt install netexec sliver`; `lifespan_helpers.py:320` "sudo apt install netexec" | neither package exists in Ubuntu/Debian archives — Kali-only |
| 7 | `config.py:760-773` derives `SCRIPT_SERVER_URL=https://<C2_IP>:3000` | dedicated listener is 4000 (`config.py:313`), and every inventory says `:4000` (`config/inventory.yml:8`, `cde26_inventory.yml:8`) |
| 8 | `.env` has `COMPETITION_START_TIME` | read by no code; real start time = playbook `start_time` / `/preflight/arm` / `/start_comp` |
| 9 | `.env` has `SLIVER_PORT` | phantom (admitted at `wiki/configuration.md:291`); real = gRPC 31337 from `sliver_operator.cfg` + `MTLS_LISTENER_PORT` |
| 10 | `.env` has `IOC_DEFINITIONS_DIR`, `VENV_DIR`, `C3R_SLIVER_TRACE*` | no Python readers; IOC dir is hard-globbed as `iocs/definitions/*.py` by `core/ioc_definition.py` |
| 11 | `CLAUDE.md:247` "`clean_stale_implants.sh` runs at start of `run.sh`" | `run.sh` never calls it; only `reset_sliver.sh:78` and `testing/run_tests.sh` do |
| 12 | `core/scheduler.py` docstring + `wiki/architecture.md:3` "APScheduler" | plain asyncio task loop; also `admin.py:834` docstring still says "cancels APScheduler job" |
| 13 | `wiki/running-competition.md:120-127` lists opt-out IOCs by `deploy_all_default=False` | no definition file sets that flag; the flags actually present in definitions are `disabled` (2), `requires_dc` (4), `pre_deployed` (3), `dynamic` (59) — verify per-release with `GET /iocs/` |
| 14 | `wiki/pre-competition.md:15` implies `scripts/setup.sh` works on a stock distro | it assumes Kali package names (§1.2) |

---

## 12. Minimal sequence for red01 / 1 team / 7 boxes (2 Windows, 4 Linux, 1 pfSense), flat L2

Assume red01 = `10.10.0.10`, boxes `10.10.0.21-22` (Win), `10.10.0.31-34` (Linux), `10.10.0.41` (pfSense); red01 reaches all of them at L2, they reach `10.10.0.10`. No AD (7-box single team → skip `promote_domain_controllers.py`, `join_linux_domain.py`, `fix_dc_clocks.py`; drop `domain_username`/`is_domain_*` so nothing attempts Kerberos).

```bash
# --- A. OS prep (Ubuntu 24.04) ---
sudo hostnamectl set-hostname red01 && exec $SHELL
sudo apt-get update && sudo apt-get -y install git openssl python3 python3-pip python3-venv \
     nmap mingw-w64 clang lld psmisc curl ca-certificates
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get -y install nodejs
sudo pip install --break-system-packages git+https://github.com/Pennyw0rth/NetExec
curl https://sliver.sh/install | sudo bash

# --- B. C2 server ---
sudo tee /etc/systemd/system/sliver.service >/dev/null <<'U'
[Unit]
Description=Sliver C2
After=network-online.target
[Service]
Type=simple
ExecStart=/usr/local/bin/sliver-server daemon
Restart=on-failure
User=root
[Install]
WantedBy=multi-user.target
U
sudo systemctl daemon-reload && sudo systemctl enable --now sliver
until timeout 1 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/31337'; do sleep 1; done   # gRPC up

# --- C. app + deps ---
git clone http://10.0.0.132:3000/hnasher1/c3rberus.git && cd c3rberus
rm -f certs/cert.pem certs/key.pem                       # drop the committed SQORECARD keypair
python3 -m venv venv && venv/bin/pip install -r requirements.txt
mkdir -p db logs implants modules
sudo -u root /usr/local/bin/sliver-server operator --name c3rberus --lhost localhost \
     --permissions all --save "$PWD/sliver_operator.cfg"
sudo chown "$USER" sliver_operator.cfg
echo 'mtls -L 0.0.0.0 -l 8888' | sudo /usr/local/bin/sliver-client console            # or let the backend do it

# --- D. config: .env (names + placeholders; keep secrets out of git) ---
python3 - <<'PY'
import re,secrets,pathlib
p=pathlib.Path('.env'); t=p.read_text()
kv={
 'JWT_SECRET_KEY':   secrets.token_urlsafe(48),
 'SCRIPT_HMAC_SECRET':secrets.token_urlsafe(48),
 'SCRIPT_ENCRYPTION_SALT': secrets.token_hex(16),
 'ENVIRONMENT':      'production',
 'DEV_MODE':         'False',
 'INVENTORY_FILE':   'config/red01_inventory.yml',
 'C2_IP':            '10.10.0.10',
 'SLIVER_HOST':      '10.10.0.10',
 'SLIVER_ENABLED':   'true',
 'MTLS_LISTENER_PORT':'8888',
 'PIVOT_LISTENER_PORT_BASE':'9900',
 'SCRIPT_SERVER_URL':'https://10.10.0.10:4000',
 'RED_SCORING_ENABLED':'true','RED_SCORING_USERNAME':'red_scoring','RED_SCORING_PASSWORD':'<SCORING_PW>','RED_SCORING_DOMAIN':'',
 'DEFAULT_WINDOWS_USERNAME':'.\\Administrator','DEFAULT_WINDOWS_PASSWORD':'<WIN_PW>',
 'DEFAULT_LINUX_USERNAME':'root','DEFAULT_LINUX_PASSWORD':'<LINUX_PW>',
 'CHECK_INTERVAL_MINUTES':'5',
 # keep the environment untouched until T0 (§3.3)
 'SA_COLLECTION_ENABLED':'false','NMAP_HEALTH_ENABLED':'false',
 'CRED_REVALIDATION_ENABLED':'false','PIVOT_KEEPALIVE_ENABLED':'false',
 'DISPLAY_TIMEZONE':'America/New_York','LOG_LEVEL':'INFO',
}
for k,v in kv.items():
    pat=re.compile(r'^%s=.*$'%k, re.M)
    t = pat.sub('%s=%s'%(k,v), t) if pat.search(t) else t+'\n%s=%s\n'%(k,v)
p.write_text(t)
PY

# --- E. inventory (7 boxes, 1 team) ---
mkdir -p config && cat > config/red01_inventory.yml <<'Y'
c2_settings:
  script_server_url: https://10.10.0.10:4000
  sliver_host: 10.10.0.10
  c2_ip: 10.10.0.10
scoring_account: { username: red_scoring, domain: "", enabled: true }
teams:
  - team_num: 1
    name: Team 1
    boxes:
      - { name: red01-win-1, ip: 10.10.0.21, os: windows,  username: .\Administrator, password: <WIN_PW>,   port: 5985 }
      - { name: red01-win-2, ip: 10.10.0.22, os: windows,  username: .\Administrator, password: <WIN_PW>,   port: 5985 }
      - { name: red01-lin-1, ip: 10.10.0.31, os: linux,    username: root,            password: <LINUX_PW>, port: 22 }
      - { name: red01-lin-2, ip: 10.10.0.32, os: linux,    username: root,            password: <LINUX_PW>, port: 22 }
      - { name: red01-lin-3, ip: 10.10.0.33, os: linux,    username: root,            password: <LINUX_PW>, port: 22 }
      - { name: red01-lin-4, ip: 10.10.0.34, os: linux,    username: root,            password: <LINUX_PW>, port: 22 }
      - { name: red01-fw-1,  ip: 10.10.0.41, os: firewall, username: admin,           password: <FW_PW>,    port: 22 }
Y

# --- F. scoring identity on the boxes (mutating; needs the admin creds above) ---
venv/bin/pip install pywinrm                              # create_red_scoring.py dials WinRM
venv/bin/python scripts/infra/create_red_scoring.py --team 1 --dry-run
venv/bin/python scripts/infra/create_red_scoring.py --team 1

# --- G. first boot (backend only; frontend optional) ---
set -a; . ./.env; set +a; ulimit -n 8192
nohup venv/bin/uvicorn fastapi_backend.main:app --host 0.0.0.0 --port 3000 \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem \
  --backlog 4096 --limit-concurrency 500 --timeout-keep-alive 15 > logs/uvicorn_boot.log 2>&1 &
until curl -sk https://localhost:3000/docs >/dev/null; do sleep 1; done

# --- H. lock in accounts, verify access ---
API=https://localhost:3000
TOK=$(curl -sk -X POST $API/login -d 'username=admin&password=<SEE db_init.py:634>' | jq -r .token)
curl -sk -X POST $API/admin/change_password -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"username":"admin","new_password":"<NEW_ADMIN_PW>"}'
TOK=$(curl -sk -X POST $API/login -d "username=admin&password=<NEW_ADMIN_PW>" | jq -r .token)
H="Authorization: Bearer $TOK"; CT='Content-Type: application/json'
venv/bin/python scripts/diagnostic/check_access.py                        # all green
curl -sk -H "$H" $API/admin/status | jq '.components | {inventory, iocs, script_server}'
curl -sk -H "$H" -H "$CT" -X POST $API/admin/scoring-cred-preflight -d '{"team_nums":[1]}' | tee /tmp/pf.json
sleep 25 && curl -sk -H "$H" "$API/c2/tasks/$(jq -r .task_id /tmp/pf.json)" | jq '.status,.metadata_json'

# --- I. plant IOCs (3 Windows-2 + Linux-4 + firewall-1 sets ⇒ 31+32+24 candidates) ---
curl -sk -H "$H" -H "$CT" -X POST $API/admin/build-all-iocs   -d '{"include_implants":true,"force":false,"team_nums":[1]}'
curl -sk -H "$H" -H "$CT" -X POST $API/admin/deploy-all-iocs  -d '{"include_implants":true,"skip_active":true,"team_nums":[1]}'
# opt-in high-blast-radius ones by name if you want them in scope:
#  ... -d '{"include_implants":true,"team_nums":[1],"ioc_names":["linux_pam_any_password","windows_rid_hijack"]}'
curl -sk -H "$H" "$API/c2/iocs/deployed?box_ip=10.10.0.31" | jq 'length'

# --- J. open the range (T0) ---
curl -sk -H "$H" -H "$CT" -X PATCH $API/admin/settings -d '{"SA_COLLECTION_ENABLED":true,"NMAP_HEALTH_ENABLED":true}'
curl -sk -H "$H" -H "$CT" -X POST $API/admin/start_comp            # scheduler fires cycle #1 immediately
#   …for a wall-clock start instead:
# curl -sk -H "$H" -X POST $API/admin/preflight/start
# curl -sk -H "$H" -H "$CT" -X POST $API/admin/preflight/arm -d '{"start_time":"2026-03-01T09:00:00"}'
watch -n30 "curl -sk $API/scoreboard | jq -c '.teams[]? // .message'"
curl -sk -H "$H" "$API/details?team_num=1" | jq '.[0:3]'
curl -sk -H "$H" $API/admin/status | jq '.components.scheduler'

# --- K. end + harvest ---
curl -sk -H "$H" -X POST $API/admin/stop_comp
cp -a db/database.db* /aar/$(date +%Y%m%d)/ ; cp -a logs /aar/$(date +%Y%m%d)/
sqlite3 /aar/…/database.db "SELECT team_num, SUM(points) FROM score_history GROUP BY 1;" > scores.csv
```

**Verify-before-T0 checklist:** `GET /c2/server-info` → `c2_ip=10.10.0.10`, `dev_mode=false`; `GET /admin/status` → `iocs.loaded ≈ 86`, `by_os {windows:32, linux:31, firewall:24}`, `scheduler.running=false`, `script_server.url` on `:4000`; from a Linux box, `curl -k https://10.10.0.10:4000/` reachable (that's the IOC script path); from the pfSense box, `nc -vz 10.10.0.10 8888`. Red team: `iptables -L C3R_SCORING -n` should show the `:4000` ACCEPT entries (`lifespan_helpers.py:837-858`).
