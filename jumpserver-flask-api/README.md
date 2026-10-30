# Jump Server DBA Ops API (Flask)

A small Flask service that runs **on the jump server** (RHEL 7.9, Python 3.8, Flask 3.0.3).
Your agent harness on another host sends it an HTTP request with a session token. The service:

1. switches to oracle with `sudo su - oracle`,
2. runs `ccps` (`eval \`ssh-agent\`; ssh-add ~/.ssh/cp993`) to load the passwordless key,
3. runs `ssh <host> /dba/bin/<script>` on the target DB server,
4. returns the script's stdout and exit code as JSON so your agent can reason over it.

```
 agent harness host                     jump server (RHEL 7.9)                      DB server
+------------------+   HTTP + token   +-----------------------------+    ssh     +----------------+
| your agent / LLM | ---------------> | gunicorn + Flask (dbaops)   | ---------> | /dba/bin/script |
|                  | <--------------- |  sudo su - oracle -c '...'  | <--------- |  stdout, rc     |
+------------------+   JSON output    |   ccps ; ssh host script    |            +----------------+
                                      +-----------------------------+
```

The agent can only run scripts listed in `config.json`. There is no free-form command field, the
host is checked against a hostname pattern, and scripts marked `state_changing` need `"confirm": true`.

## What actually runs

For `{"host": "dbprod01", "script": "dba_checkout"}` the service (running as the `dbaops` user)
executes `sudo -n su - oracle -c '<script>'`, where the script given to the oracle shell is:

```sh
trap '[ -n "$SSH_AGENT_PID" ] && ssh-agent -k >/dev/null 2>&1' EXIT
{ eval `ssh-agent -s`; ssh-add ~/.ssh/cp993 ; } </dev/null >&2 || { echo __DBAOPS_CCPS_FAILED__ >&2; exit 97; }
echo __DBAOPS_SSH_START__; echo __DBAOPS_SSH_START__ >&2
timeout 600 ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o StrictHostKeyChecking=yes dbprod01 /dba/bin/dba_checkout </dev/null
```

Line 1 kills this request's `ssh-agent` on exit, line 2 is `ccps`, line 3 marks where the returned
output starts, and line 4 runs the script on the target with a time limit.

Notes on why it is built this way:

- `sudo su - oracle` on its own opens an interactive shell and waits for the keyboard. Passing the
  commands with `-c` runs them in the same oracle login shell, which is the non-interactive form.
- `ccps` is usually an alias in oracle's profile. Aliases are not expanded in a non-interactive
  shell, so the real command is in `config.json` (`"ccps"`).
- `ssh-agent` only lives in the shell that started it, so `ccps` and `ssh` run in the same shell,
  and the agent is killed on exit so requests do not leave `ssh-agent` processes behind.
- Anything your `.bash_profile` prints (banners, `oraenv` messages) comes before the marker and is
  dropped from `output`.
- `sudo -n` makes sudo fail immediately instead of hanging if it would ask for a password.
- The shell is POSIX `sh`, so it also works if oracle's login shell is `ksh`.

Print the exact command for any request without running it:

```bash
python3.8 -m dbaops_api dry-run --host dbprod01 --script dba_checkout
```

---

## Step-by-step installation on the jump server

All commands run on the jump server. Steps marked **root** need root or sudo.

### Step 1. Check the manual path works first

Before installing anything, confirm the path the service will automate works by hand:

```bash
sudo su - oracle
eval `ssh-agent`; ssh-add ~/.ssh/cp993          # ccps
ssh -o BatchMode=yes dbprod01 /dba/bin/dba_checkout
echo "exit code: $?"
ssh-agent -k
exit
```

You must not be asked for a password, passphrase, or "Are you sure you want to continue connecting".
If you are, fix it now:

- **Host key prompt**: `BatchMode=yes` and `StrictHostKeyChecking=yes` refuse unknown hosts. RHEL 7's
  OpenSSH 7.4 does not support `accept-new`, so add the keys once as oracle
  (`ssh-keyscan dbprod01 >> ~/.ssh/known_hosts`, and check the fingerprints), or ssh to each host by
  hand one time.
- **Passphrase on cp993**: `ssh-add` cannot prompt without a terminal. The key must have no passphrase,
  or `ccps` must load it some other way.

### Step 2. Create the service account (root)

```bash
useradd -r -m -d /opt/dbaops-api -s /bin/bash dbaops
mkdir -p /etc/dbaops-api /var/log/dbaops-api
chown dbaops:dbaops /var/log/dbaops-api
```

If you prefer, you can run the service as an existing admin account that already has sudo. Replace
`dbaops` with that account in the steps below.

### Step 3. Allow `sudo su - oracle` without a password (root)

```bash
cd /path/to/jumpserver-flask-api
visudo -cf deploy/sudoers-dbaops
install -m 440 -o root -g root deploy/sudoers-dbaops /etc/sudoers.d/dbaops
```

The file contains:

```
Defaults:dbaops !requiretty
dbaops ALL=(root) NOPASSWD: /bin/su - oracle -c *, /usr/bin/su - oracle -c *
```

Test it:

```bash
sudo -u dbaops sudo -n su - oracle -c 'whoami; ls -l ~/.ssh/cp993'
```

It should print `oracle` and the key file. If it prints `sudo: a password is required` or
`sudo: sorry, you must have a tty to run sudo`, the sudoers rule did not apply.

### Step 4. Copy the code (root)

```bash
cp -r jumpserver-flask-api/. /opt/dbaops-api/
chown -R dbaops:dbaops /opt/dbaops-api
```

### Step 5. Create a virtual environment with Python 3.8

```bash
sudo -iu dbaops
cd /opt/dbaops-api
/usr/local/bin/python3.8 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt      # Flask==3.0.3, gunicorn==23.0.0
venv/bin/python -c "import flask, gunicorn; print('ok')"
```

**If the jump server has no internet access**: on any machine with Python 3.8 and internet, run
`pip download -r requirements.txt -d wheels`, copy `wheels/` over, then
`venv/bin/pip install --no-index --find-links wheels -r requirements.txt`. All the packages are pure
Python, so the wheels work on RHEL 7.

### Step 6. Configure the scripts

```bash
cp config.example.json config.json
vi config.json
```

The main settings:

| key | meaning |
| --- | --- |
| `script_dir` | `/dba/bin`. Every script runs as `<script_dir>/<file>` on the target. |
| `become` | How to become oracle. Default `["sudo","-n","su","-","oracle","-c"]`. Use `[]` if the service already runs as oracle. |
| `ccps` | The real ccps command, run in the oracle shell before ssh. |
| `ssh_user` | `null` connects as oracle (the current user). Set a name for `user@host`. |
| `ssh_options` | Extra `ssh` options. Keep `BatchMode=yes` so ssh never waits for a password. |
| `host_pattern` | Regex every `host` must match. Tighten it to your naming standard, for example `^db[a-z0-9-]+(\\.corp\\.example\\.com)?$`. |
| `allowed_hosts` | Optional exact list. When it is not empty, only these hosts are accepted. |
| `scripts` | The allow-list (see below). |
| `default_timeout` | Seconds a script may run (default 600). Can be overridden per script. |
| `max_concurrent` | Scripts running at the same time per worker. Extra requests get `429`. |
| `session_ttl_seconds` | How long a session token is valid (default 8 hours). |
| `audit_log` | JSON-lines file recording every session and run. |
| `llm` | Only used when a request sends `"analyze": true`. Any OpenAI-compatible `/chat/completions` endpoint. |

Each script entry (the `//` notes are explanations only; JSON does not allow comments):

```jsonc
"maint_mode": {
  "file": "maint_mode.sh",            // file name inside /dba/bin, no slashes
  "args": ["-s"],                     // fixed arguments; {hostname} is replaced with the host
  "description": "Put the host into maintenance mode",
  "state_changing": true,             // request must send "confirm": true, otherwise 409
  "pass_hostname": false,             // true appends the host as the last argument
  "timeout": 900                      // optional, seconds
}
```

`/scripts` and `/tools` are generated from this list, so your agent always sees the current allow-list.
JSON has no comments: keys that start with `_` are ignored, so you can use `"_comment"`.

### Step 7. Set the secrets (root)

```bash
/opt/dbaops-api/venv/bin/python -m dbaops_api gen-secret     # run twice: one per secret
cp /opt/dbaops-api/deploy/dbaops.env.example /etc/dbaops-api/dbaops.env
vi /etc/dbaops-api/dbaops.env
chown root:dbaops /etc/dbaops-api/dbaops.env && chmod 640 /etc/dbaops-api/dbaops.env
```

- `DBAOPS_SECRET_KEY` signs the session tokens. Changing it revokes every token at once.
- `DBAOPS_API_KEY` is a long-lived key the harness sends to `POST /session` to get a fresh token. Leave
  it empty if you would rather mint tokens on the jump server and paste them into the harness.

### Step 8. Test from the command line

```bash
sudo -iu dbaops
cd /opt/dbaops-api
set -a; . /etc/dbaops-api/dbaops.env; set +a     # dbaops can read it (group dbaops, mode 640)
venv/bin/python -m dbaops_api check
venv/bin/python -m dbaops_api dry-run --host dbprod01 --script dba_checkout
venv/bin/python -m dbaops_api serve --host 127.0.0.1 --port 8080
```

In a second terminal on the jump server, again as dbaops with the env file loaded:

```bash
sudo -iu dbaops
cd /opt/dbaops-api && set -a; . /etc/dbaops-api/dbaops.env; set +a
TOKEN=$(venv/bin/python -m dbaops_api token)
curl -s -X POST http://127.0.0.1:8080/run \
  -H "X-Session-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"host":"dbprod01","script":"dba_checkout"}'
```

Press Ctrl+C in the first terminal when it works. `serve` uses Flask's built-in server, which is
fine for testing. Use gunicorn (next step) for the real service.

### Step 9. Run it as a systemd service (root)

```bash
cp /opt/dbaops-api/deploy/dbaops-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dbaops-api
systemctl status dbaops-api
journalctl -u dbaops-api -f
curl -s http://127.0.0.1:8080/health
```

gunicorn's `--timeout 960` must be longer than your longest script timeout plus 30 seconds.
Do not add `NoNewPrivileges=yes` to the unit, because it stops sudo from working.

### Step 10. Open the port only to the agent harness host (root)

The harness is on another host, so the service listens on `0.0.0.0:8080`. Allow only that host:

```bash
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.20.30.40/32" port port="8080" protocol="tcp" accept'
firewall-cmd --reload
```

Replace `10.20.30.40` with the harness host's IP. Do not open 8080 to the whole zone.

**HTTPS (recommended)**: the token travels in a header, so use TLS when the harness is not on a
trusted network. Add `--certfile /etc/dbaops-api/tls.crt --keyfile /etc/dbaops-api/tls.key` to
`ExecStart` in the unit file, or put nginx in front of the service. SELinux needs no change for gunicorn
started by systemd on port 8080.

### Step 11. Call it from the agent harness host

```bash
export JUMP=http://jumpserver.example.com:8080

# get a session token (or use one minted on the jump server with "dbaops_api token")
TOKEN=$(curl -s -X POST $JUMP/session -H "X-API-Key: $DBAOPS_API_KEY" \
          -H 'Content-Type: application/json' -d '{"client":"agent-harness"}' \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

curl -s -X POST $JUMP/run \
  -H "X-Session-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"host":"dbprod01","script":"dba_checkout","confirm":false,"analyze":false}'
```

---

## API reference

All endpoints except `/health` and `/session` need `X-Session-Token: <token>` (or
`Authorization: Bearer <token>`).

| method | path | purpose |
| --- | --- | --- |
| GET | `/health` | Liveness check, no token. |
| POST | `/session` | Header `X-API-Key`. Returns `{"token", "expires_in", "expires_at"}`. |
| GET | `/scripts` | The allow-listed scripts, with path, args, `requires_confirm`, timeout. |
| GET | `/tools` | The same list as an OpenAI-style function tool (`run_dba_script`) for your LLM. |
| POST | `/run` | Run one script on one host. |

### POST /run

Headers: `X-Session-Token: b_bKfGkc9dZIMkQ6GArwara9c-w0QIueIhkrOrjRYlg`, `Content-Type: application/json`

```json
{ "host": "dbprod01", "script": "dba_checkout", "confirm": false, "analyze": false }
```

| field | required | notes |
|-------|----------|-------|
| `host` | yes | Target DB server, checked against `host_pattern` (and `allowed_hosts` if set). |
| `script` | yes | One of the allow-listed names (see `/scripts` or `/tools`). |
| `confirm` | only for state-changing scripts | Must be `true` to run `start_oracle` / `maint_mode`. |
| `analyze` | no | `true` makes the service call the configured LLM and add `analysis`. Leave `false` and let your own agent reason over `output`. |
| `dry_run` | no | `true` returns the command that would run, without connecting. |

Response `200`:

```json
{
  "host": "dbprod01",
  "script": "dba_checkout",
  "command": "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o StrictHostKeyChecking=yes dbprod01 /dba/bin/dba_checkout",
  "exit_code": 0,
  "output": "...raw script stdout...",
  "stderr": "",
  "status": "ok",
  "duration_s": 12.4
}
```

A script that runs but exits non-zero still returns `200`, with that `exit_code` and
`"status": "script_failed"`, so your agent sees the output (check scripts often exit non-zero on warnings).

Errors (always JSON with an `error` field):

| code | when |
| --- | --- |
| `400` | Bad host, unknown script, body is not JSON, `confirm`/`analyze` not a boolean, or `analyze: true` with no LLM configured. |
| `401` | Missing, invalid, or expired session token (or wrong `X-API-Key` on `/session`). |
| `409` | State-changing script without `"confirm": true`. |
| `429` | `max_concurrent` scripts are already running. Retry. |
| `502` | Jump server or connection problem. `status` says which: `become_failed` (sudo/su), `ccps_failed` (ssh-agent/ssh-add), `ssh_failed` (ssh exit 255: unreachable, host key, auth), `timeout`. The body includes `error` with the message from the jump server. |

### Using it from your agent

`examples/agent_client.py` is a standard-library client for the harness host. It renews the token
automatically on `401` when `DBAOPS_API_KEY` is set:

```python
from agent_client import DbaOpsClient, handle_tool_call

client = DbaOpsClient("http://jumpserver:8080", api_key=os.environ["DBAOPS_API_KEY"])
tools = client.tools()                 # hand these to your LLM as its tool list
result = client.run("dbprod01", "dba_checkout")
print(result["exit_code"], result["output"])

# when the LLM calls run_dba_script, pass its arguments string through:
tool_output = handle_tool_call(client, tool_call.function.arguments)
```

Keep a person in the loop for state-changing scripts: have the harness ask for approval before it
sends `"confirm": true`, rather than letting the LLM set it on its own.

---

## Security summary

- Only allow-listed script names run. Script `file` values cannot contain `/`, so nothing outside
  `/dba/bin` can be reached.
- `host` must match `host_pattern`, which cannot start with `-`, so it can never be read as an ssh
  option. Every value is shell-quoted.
- `sudo -n` never prompts. The sudoers rule only allows `su - oracle -c ...`.
- Tokens are signed and expire (`session_ttl_seconds`). Rotate `DBAOPS_SECRET_KEY` to revoke all.
- Every session and run is written to `audit_log` with the client name, source IP, host, script,
  status, and exit code.
- Restrict port 8080 to the harness host with firewalld, and use TLS.

## Troubleshooting

| symptom | cause and fix |
| --- | --- |
| `502 become_failed`, `sudo: a password is required` | The sudoers rule is missing or the service runs as a different user. Repeat step 3. |
| `502 become_failed`, `you must have a tty to run sudo` | Add `Defaults:dbaops !requiretty` (in `deploy/sudoers-dbaops`). |
| `502 ccps_failed`, `Could not open a connection to your authentication agent` | `ssh-agent` did not start. Check the `ccps` value in `config.json`. |
| `502 ccps_failed`, `No such file` or `bad permissions` | `~oracle/.ssh/cp993` is missing or not mode 600. |
| `502 ssh_failed`, `Host key verification failed` | Add the target to `~oracle/.ssh/known_hosts` (step 1). |
| `502 ssh_failed`, `Permission denied (publickey)` | cp993's public key is not in the target's `authorized_keys`. |
| `502 timeout` | The script ran longer than its `timeout`. Raise it for that script, and keep gunicorn's `--timeout` higher. |
| `output` contains banner text | The banner is printed by the *target's* login, not the jump server's. Remove it there or filter it in your agent. |
| `pip install` fails with `SSL module is not available` | Python 3.8 was built without `openssl-devel`. Rebuild it, or use the offline wheel install in step 5. |
| Requests hang about 30 seconds, then gunicorn kills the worker | gunicorn's default timeout is 30 seconds. Use the unit file in `deploy/`, which sets `--timeout 960`. |

## Tests

```bash
venv/bin/pip install pytest
venv/bin/python -m pytest -q
```

`tests/test_shell.py` runs the generated oracle shell for real, with `ssh-agent`, `ssh-add`, and
`timeout`, against a stub `ssh`. It checks that the key is loaded, exit codes are kept, and no
`ssh-agent` is left running.
