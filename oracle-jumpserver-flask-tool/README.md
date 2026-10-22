# Oracle jump-server Flask tool

A small HTTP service you run on your own Windows laptop. Your LLM agent calls it as a
function-calling tool to run a fixed set of Oracle DBA scripts on database servers,
through the jump server, as `oracle`.

```
LLM agent --HTTP (127.0.0.1:5055, API key)--> this tool on your laptop
   --SSH (paramiko, your username/password)--> jump server
       sudo su - oracle   (your password, if sudo asks)
       ccps               (ccps password, default bake6Pies)
       ssh <host> /dba/bin/<script>   --> database server
```

You log in once (console prompt, web page or `POST /login`). The tool keeps one interactive
shell open on the jump server as `oracle`, so every agent call is a quick `ssh <host> <script>`.

Only these commands can run, chosen by name:

| name | command |
| --- | --- |
| `showsid` | `/dba/bin/showsid` |
| `showusers` | `/dba/bin/showusers` |
| `dba_checkout` | `/dba/bin/dba_checkout` |
| `start_oracle` | `/dba/bin/start_oracle` |
| `maint_mode` | `/dba/bin/maint_mode.sh -s` |

## Install on Windows

You need Python 3.10 or newer from [python.org](https://www.python.org/downloads/) (tick
**Add python.exe to PATH**).

```bat
cd oracle-jumpserver-flask-tool
install.bat
notepad .env          rem check JT_JUMP_HOST; optionally set JT_API_KEY
run.bat
```

`install.bat` creates `.venv`, installs `requirements.txt` and copies `.env.example` to `.env`.
`run.bat` asks for the jump server, username and password, logs in, then prints the URL and the
API key and starts serving:

```
Jump server [pretzel.int.thomsonreuters.com]:
Username: jdoe
Password (input hidden):
Connecting, switching to oracle and running ccps ...
Logged in as jdoe@pretzel.int.thomsonreuters.com. Steps: shell ready, sudo password sent, became oracle, ccps password sent, ccps done (exit 0)
====================================================================
 Oracle jump-server tool 0.1.0  ->  http://127.0.0.1:5055
 API key (new each start; set JT_API_KEY in ...\.env to fix it):
   hnz6bd4MM1pmxKd9mh2WINbkHoz31enq3PbFzA0WaEU
====================================================================
```

Press Enter at `Username:` to skip the console login and use http://127.0.0.1:5055/ instead.
`run.bat --no-prompt` always skips it; `run.bat --port 5056` changes the port.

On Linux or macOS: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && python -m oracle_jump_tool`.

### Single .exe (optional)

`build_exe.bat` installs PyInstaller and builds `dist\oracle-jump-tool.exe` from
`oracle_jump_tool.spec`. Copy the exe to any Windows laptop (no Python needed), put a `.env`
next to it, and double-click it. Build it on Windows; PyInstaller does not cross-compile.

## Using it

1. Start the tool and log in (console, web page, or the API below).
2. Give your agent the base URL `http://127.0.0.1:5055`, the API key, and the tool schema.
3. The session lasts until you log out, stop the tool, 30 minutes without a call, or 8 hours.
   After that the agent gets `409 not_logged_in` and should ask you to log in again.

### Endpoints

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `GET /health` | none | `{"status":"ok","logged_in":true,...}` |
| `GET /` | none | Login page |
| `POST /login` | none (needs jump credentials) | `{"username","password","jump_host"?,"jump_port"?}` returns a session `token` |
| `POST /logout` | key | Closes the SSH session |
| `GET /session` | key | Session details and expiry |
| `GET /scripts` | none | The allowlist |
| `POST /run` | key | `{"script":"showsid","host":"db01"}` or `"hosts":["db01","db02"]`, optional `timeout_s` |
| `GET /openapi.json` | none | OpenAPI 3 spec |
| `GET /tool-schema.json` | none | OpenAI-style tool definitions |

"key" means `Authorization: Bearer <key>` (or `X-API-Key: <key>`), where the key is either the
API key printed at startup (or `JT_API_KEY`), or the session token returned by `/login`.

`/run` response:

```json
{
  "ok": false,
  "script": "showsid",
  "command": "/dba/bin/showsid",
  "session_closed": false,
  "results": [
    {"host": "db01", "script": "showsid", "status": "ok", "exit_code": 0,
     "stdout": "SID      STATUS\nORCL1    OPEN\n", "stderr": "", "duration_s": 1.42,
     "started_at": "2026-10-22T18:02:14+0000", "truncated": false, "error": null,
     "command": "ssh -n -T -o BatchMode=yes ... -- db01 /dba/bin/showsid"},
    {"host": "db02", "status": "connection_error", "exit_code": 255, "stderr": "ssh: Could not resolve hostname db02 ...", "...": "..."}
  ]
}
```

`status` is `ok` (exit 0), `failed` (non-zero exit), `connection_error` (ssh exit 255),
`timeout` (the tool pressed Ctrl-C) or `error` (the shell broke; `session_closed` is then true).
Several hosts run one after another on the same shell. If a second `/run` arrives while one is
running, it waits (up to `JT_BUSY_WAIT_SECONDS`) and then gets `503 busy`.

Errors are JSON `{"error": "<code>", "message": "..."}`: `400 invalid_request` (unknown script,
bad hostname, too many hosts), `401 unauthorized`, `409 not_logged_in`, `503 busy`, and for
login `401 login_failed` (wrong password) or `502 login_failed` with `stage` and a redacted
`transcript` of the jump server output.

### curl

```bat
set K=<API key>
curl -s http://127.0.0.1:5055/health
curl -s -X POST http://127.0.0.1:5055/login -H "Content-Type: application/json" -d "{\"username\":\"jdoe\",\"password\":\"...\"}"
curl -s -X POST http://127.0.0.1:5055/run -H "Authorization: Bearer %K%" -H "Content-Type: application/json" -d "{\"host\":\"proddb01\",\"script\":\"showsid\"}"
curl -s -X POST http://127.0.0.1:5055/run -H "Authorization: Bearer %K%" -H "Content-Type: application/json" -d "{\"hosts\":[\"proddb01\",\"proddb02\"],\"script\":\"maint_mode\"}"
curl -s -X POST http://127.0.0.1:5055/logout -H "Authorization: Bearer %K%"
```

(In PowerShell use `curl.exe` and single quotes around the JSON.)

### Python and agent integration

- OpenAPI 3: [`oracle_jump_tool/specs/openapi.json`](oracle_jump_tool/specs/openapi.json), also served at `/openapi.json`.
- OpenAI-style tools: [`oracle_jump_tool/specs/tool_schema.json`](oracle_jump_tool/specs/tool_schema.json):
  `run_oracle_script(script, hosts, timeout_s?)`, `list_oracle_scripts()`, `get_jump_session_status()`.
- [`agent/example_client.py`](agent/example_client.py): `dispatch(name, arguments)` maps a tool call to HTTP, plus a minimal OpenAI tool loop.

```python
import json, urllib.request

def run_oracle_script(script, hosts, key, base="http://127.0.0.1:5055"):
    req = urllib.request.Request(f"{base}/run", method="POST",
        data=json.dumps({"script": script, "hosts": hosts}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3700) as r:
        return json.load(r)

print(run_oracle_script("showsid", ["proddb01"], key="<API key>"))
```

Set your agent's HTTP timeout above the script timeout (default 900 s, 1800 s for
`dba_checkout` and `start_oracle`). The tool never sends passwords to the agent; tell the
agent to ask the user to log in when it sees `not_logged_in`.

## How the login dialog works

All prompts are matched with regexes you can override in `.env`:

| Step | Sent | Waits for (default regex) |
| --- | --- | --- |
| SSH | your username/password (paramiko, no keys or agent) | shell prompt `[$#>%]\s*$`, or 2 s of silence |
| become | `sudo su - oracle` | sudo prompt `[sudo] password for <user>:` or `Password:`; then your password once. `Sorry, try again` fails the login |
| check | `printf` marker with `$(id -un)` | must print `oracle` (`JT_BECOME_USER`) |
| setup | `stty -echo; unset HISTFILE` | marker |
| ccps | `ccps` | any line ending in `password ...:` or `passphrase ...:`; sends `JT_CCPS_PASSWORD` (default `bake6Pies`), up to 3 times. `Bad passphrase` fails. No prompt is fine unless `JT_CCPS_REQUIRE_PROMPT=true` |
| ccps result | `printf` marker with `$?` | must be 0 (`JT_CCPS_IGNORE_EXIT_CODE=true` to skip) |
| run | `ssh -n -T -o BatchMode=yes ... -- <host> <script>` with stderr to a temp file | start/stderr/end markers carrying a random nonce and the exit code |

Each marker is printed with `printf '__JT_X_%s__' <nonce>`, so the echoed command line never
matches; only real output does. If no known prompt appears, the tool waits for 10 s of silence
(`JT_QUIET_SECONDS`) before moving on, so a slow sudo or ccps prompt is not answered with the
next command. The oracle login shell must be sh, bash or ksh (not csh).

## Configuration

Everything is in `.env` (see [`.env.example`](.env.example)) or environment variables. The
important ones: `JT_JUMP_HOST`, `JT_API_KEY`, `JT_CCPS_PASSWORD`, `JT_ALLOWED_HOST_REGEX`,
`JT_SESSION_IDLE_TIMEOUT` (1800), `JT_SESSION_MAX_AGE` (28800), `JT_COMMAND_TIMEOUT` (900),
`JT_HOST_KEY_POLICY` (`tofu`), `JT_TARGET_SSH_OPTIONS`.

## Security

- **Your password** is typed at runtime and kept only in memory, inside the paramiko session.
  It is not written to disk or logs, and it is masked (`[redacted]`) in every output, transcript
  and error. After login only the open SSH connection is kept.
- **ccps password**: `bake6Pies` is the default in code as requested. Override it with
  `JT_CCPS_PASSWORD`. It is masked in output too.
- **Allowlist**: the agent picks a script *name*. The request cannot add arguments, paths or
  shell text. Hostnames must be valid DNS names or IP addresses (no spaces, `;`, `|`, `$`,
  backticks, quotes, or a leading `-`), are passed after `--`, and are quoted with `shlex`.
  `JT_ALLOWED_HOST_REGEX` limits targets further; the jump server itself is refused.
- **Network**: binds to `127.0.0.1` only. Requests whose `Host` header is not
  `127.0.0.1`/`localhost` are refused (blocks DNS-rebinding pages), cross-site `Origin`s are
  refused on POST, and CORS is not enabled, so web pages cannot call the API with your key.
- **API key**: random per start unless `JT_API_KEY` is set; compared in constant time. Any
  program on the laptop that has the key can run the allowlisted scripts while you are
  logged in, so treat it like a password.
- **Timeouts**: 30 min idle and 8 h maximum session, per-command timeouts, and a reaper thread
  that closes expired sessions. Stopping the tool closes the SSH session.
- **Host keys**: `tofu` saves the jump server key to `known_hosts` next to the tool on first
  login and refuses a changed key afterwards. Use `strict` with a pre-filled known_hosts file
  if you can.
- `start_oracle` changes state. The tool schema tells the model to confirm with the user first;
  remove it from `ALLOWED_SCRIPTS` in `oracle_jump_tool/scripts.py` if the agent should never
  run it.

## Alternative: run Flask on the jump server

Instead of each laptop SSHing to the jump server, the same app could run on the jump server
(or a utility host next to it) and run `ccps`/`ssh` locally.

Pros

- No SSH hop or paramiko session per user; one long-lived service, simpler for many agents.
- No Python install on laptops; agents in the cloud or on other servers can reach it.
- Lower latency, and the service can open several oracle shells and run hosts in parallel.

Cons

- It becomes a network service that runs commands as `oracle` on every DB server: a much bigger
  target. It needs real authentication, TLS, auditing and change control.
- Per-person identity is lost unless you add SSO/OIDC or mTLS and map users. With the laptop
  model, each run is under the person's own login and sudo.
- The ccps password and key would live on the server (service account, vault), not be typed.
- Needs approval from whoever owns the jump server, plus firewall changes.

How

- Run it under gunicorn as a dedicated account that may run `ccps` and `ssh` as oracle (or as
  oracle itself), managed by systemd, for example:

  ```ini
  # /etc/systemd/system/oracle-jump-tool.service
  [Service]
  User=oracle
  EnvironmentFile=/etc/oracle-jump-tool.env
  ExecStart=/opt/oracle-jump-tool/.venv/bin/gunicorn -w 1 --threads 8 -b 127.0.0.1:5055 --timeout 3700 wsgi:app
  Restart=on-failure
  NoNewPrivileges=true
  ```

  [`wsgi.py`](wsgi.py) builds the app from `.env`. Keep one worker: sessions live in process
  memory. The current code drives a remote interactive shell; on the jump server you would
  swap `JumpShell` for a local `subprocess` runner (`ssh -- host script` as an argument list)
  and replace the login step with service-level auth.
- Put nginx (or another reverse proxy) in front with TLS (internal CA), bind gunicorn to
  127.0.0.1, and restrict source IPs with a firewall.
- Auth: per-agent API keys stored hashed, or OIDC / mTLS client certificates; rate limiting;
  an audit log of who ran what on which host. Keep the same allowlist and hostname checks.
- Network: the LLM side must be able to reach the jump server over HTTPS. A cloud-hosted LLM
  usually cannot reach an internal jump server, so the agent has to run inside the network, or
  you need an approved gateway/tunnel. That reachability is often the deciding factor, and the
  reason this repo defaults to the laptop model, where the agent and the tool are on the same
  machine.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests (Linux/macOS, they need a pty) run a real `bash` as the fake jump server with fake
`sudo`, `id`, `ccps` and `ssh` commands from `tests/fakebin/`, with and without a paramiko
SSH server in front. They cover the sudo and ccps prompts, wrong passwords, slow and late
prompts, stdout/stderr/exit-code splitting, Ctrl-C after a timeout, secret masking, auth,
validation, timeouts, host checks and concurrent runs. For a manual demo:
`python tests/fake_ssh_server.py --port 2222 --sudo-password`, then log in to `127.0.0.1:2222`
as `jumpuser` / `S3cret-user-pw`.
