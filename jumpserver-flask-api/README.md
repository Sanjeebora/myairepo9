# Jump Server DBA Ops API (Flask)

A small Flask service that runs **on the jump server** (RHEL 7.9, Python 3.8, Flask 3.0.3).
Each person logs in with **their own jump server username and password**. The agent harness
then sends requests with that person's session token, and the service:

1. uses the person's own SSH login to the jump server,
2. runs `sudo su - oracle` with **their** sudo rights (normally passwordless; if sudo does ask, it answers with their password),
3. runs `ccps` (`eval \`ssh-agent\`; ssh-add ~/.ssh/cp993`), answering ssh-add's passphrase prompt with `CCPS_PASSPHRASE`,
4. runs `ssh <host> /dba/bin/<script>` on the target DB server,
5. returns the script's stdout and exit code as JSON so your agent can reason over it.

```
 agent harness host                       jump server (RHEL 7.9)                           DB server
+-------------------+  HTTPS             +------------------------------------------+  ssh  +-----------------+
| person logs in    | -- POST /session ->| Flask (runs as dbaops, no sudo rights)   |       |                 |
| user + password   | <-- token -------- |   opens SSH to localhost AS THAT PERSON  |       |                 |
|                   |                    |                                          |       |                 |
| agent / LLM       | -- POST /run ----> |   jsmith$ sudo su - oracle -c '          | ----> | /dba/bin/script |
|                   | <-- JSON output -- |      ccps; ssh host /dba/bin/script'     | <---- |  stdout, rc     |
+-------------------+                    +------------------------------------------+       +-----------------+
```

Because every command runs inside the person's own login, the jump server's normal records
(`/var/log/secure` for the SSH login and each `sudo`) show who ran what. The service's own audit
log records the same name. The service account itself has no sudo rights.

The agent can only run scripts listed in `config.json`. There is no free-form command field, the
host is checked against a hostname pattern, and scripts marked `state_changing` need `"confirm": true`.

## How a login works

1. `POST /session` with `{"username": "jsmith", "password": "..."}`.
2. The service opens an SSH connection to the jump server (`127.0.0.1:22` by default) as `jsmith`
   with that password. sshd and PAM check it exactly as if jsmith had typed `ssh jsmith@jumpserver`,
   so the normal password policy applies, including expiry, account lockout, and LDAP/AD through sssd.
   Both `password` and `keyboard-interactive` sshd setups work.
3. On success the service returns a random session token. The SSH connection stays open and is
   reused by that person's `/run` calls. Nothing is written to disk.
4. The session ends on `DELETE /session`, after `session_idle_seconds` of no use (30 minutes by
   default), after `session_ttl_seconds` (8 hours by default), or when the service restarts. After that
   the person logs in again.

Wrong passwords: five failures in ten minutes for the same username or the same source IP gives
`429` until the window passes. The failures also count toward the jump server's own PAM lockout
(`pam_faillock`), just like failed SSH logins.

The password is kept in memory only while the session is open, so the SSH login can be reopened
and sudo answered if it ever asks. It is never logged, and it is masked as `********` if it ever
appears in script output.

## The ccps passphrase

`ccps` runs `ssh-add ~/.ssh/cp993`, which asks for the key's passphrase. There is no terminal, so
the service supplies it:

- The passphrase is read from the `CCPS_PASSPHRASE` environment variable, which systemd loads from
  `/etc/dbaops-api/dbaops.env` (readable by root and the `dbaops` group only). It is the same for
  everyone, because it belongs to oracle's key, not to a person.
- For each run, the oracle shell prints a marker, and the service writes the passphrase to the SSH
  session's input. The shell gives it to `ssh-add` through a one-time `SSH_ASKPASS` helper in a
  private temporary directory, which is deleted before `ssh` starts. The passphrase never appears on a
  command line, so `ps` cannot show it. It is not passed on to `ssh` or the target, and it is masked in
  returned output.
- A wrong passphrase fails at once with `502 ccps_failed`. The helper answers only once, so `ssh-add`
  cannot keep retrying.
- Leave `CCPS_PASSPHRASE` empty if cp993 has no passphrase.

Keep the real value out of git (this repository is public). Put it only in the env file on the jump
server (step 10).

## What actually runs

For `{"host": "dbprod01", "script": "dba_checkout"}` from jsmith, the service runs this inside
jsmith's SSH login:

```
sudo -S -p __DBAOPS_SUDO_PASSWORD__: su - oracle -c '<oracle shell>'
```

and the oracle shell is:

```sh
trap '[ -n "$DBAOPS_TMP" ] && rm -rf "$DBAOPS_TMP"; [ -n "$SSH_AGENT_PID" ] && ssh-agent -k >/dev/null 2>&1' EXIT
# --- only when CCPS_PASSPHRASE is set: receive it on stdin and prepare a one-time askpass helper
DBAOPS_TMP=`mktemp -d` || exit 97
echo __DBAOPS_CCPS_PASSPHRASE__: >&2
IFS= read -r DBAOPS_CCPS_PASS || DBAOPS_CCPS_PASS=
printf '%s\n' '#!/bin/sh' ... > "$DBAOPS_TMP/askpass"      # answers once, then refuses
chmod 700 "$DBAOPS_TMP/askpass"
SSH_ASKPASS="$DBAOPS_TMP/askpass"; SSH_ASKPASS_REQUIRE=force; DISPLAY="${DISPLAY:-dbaops:0}"
export DBAOPS_TMP DBAOPS_CCPS_PASS SSH_ASKPASS SSH_ASKPASS_REQUIRE DISPLAY
# ---
{ eval `ssh-agent -s`; ssh-add ~/.ssh/cp993 ; } </dev/null >&2 || { echo __DBAOPS_CCPS_FAILED__ >&2; exit 97; }
unset DBAOPS_CCPS_PASS SSH_ASKPASS SSH_ASKPASS_REQUIRE DISPLAY; rm -rf "$DBAOPS_TMP"
echo __DBAOPS_SSH_START__; echo __DBAOPS_SSH_START__ >&2
timeout 600 ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o StrictHostKeyChecking=yes dbprod01 /dba/bin/dba_checkout </dev/null
```

The trap kills this request's `ssh-agent` and removes the helper on exit. The middle block feeds the
passphrase to `ssh-add`, the `eval ...; ssh-add` line is `ccps`, the marker shows where the returned
output starts, and the last line runs the script on the target with a time limit. Run `dry-run` to
see the exact text.

Notes on why it is built this way:

- `sudo -S -p <marker>` makes sudo print a recognisable prompt if it ever asks. With your
  passwordless `sudo su - oracle` it never does, so nothing is sent. Each answer (sudo password or
  ccps passphrase) is sent only when its own prompt appears, and input is closed before `ssh` starts.
- `sudo su - oracle` on its own opens an interactive shell and waits for the keyboard. Passing the
  commands with `-c` runs them in the same oracle login shell, which is the non-interactive form.
- `ccps` is usually an alias in oracle's profile. Aliases are not expanded in a non-interactive
  shell, so the real command is in `config.json` (`"ccps"`).
- `ssh-agent` only lives in the shell that started it, so `ccps` and `ssh` run in the same shell,
  and the agent is killed on exit so requests do not leave `ssh-agent` processes behind.
- Anything your `.bash_profile` prints (banners, `oraenv` messages) comes before the marker and is
  dropped from `output`.
- The oracle shell is POSIX `sh`, so it also works if oracle's login shell is `ksh`.

Print the exact command for any request without running it:

```bash
venv/bin/python -m dbaops_api dry-run --host dbprod01 --script dba_checkout
```

---

## Step-by-step installation on the jump server

All commands run on the jump server. Steps marked **root** need root or sudo.

### Step 1. Check the manual path works for a real person

Log in to the jump server as yourself (not root), then:

```bash
sudo su - oracle
eval `ssh-agent`; ssh-add ~/.ssh/cp993          # ccps
ssh -o BatchMode=yes dbprod01 /dba/bin/dba_checkout
echo "exit code: $?"
ssh-agent -k
exit
```

`sudo su - oracle` should not ask for a password, and `ssh-add` asks only for the cp993 passphrase
(the service supplies it from `CCPS_PASSPHRASE`). Nothing else may prompt, in particular not
"Are you sure you want to continue connecting":

- **Host key prompt**: `BatchMode=yes` and `StrictHostKeyChecking=yes` refuse unknown hosts. RHEL 7's
  OpenSSH 7.4 does not support `accept-new`, so add the keys once as oracle
  (`ssh-keyscan dbprod01 >> ~/.ssh/known_hosts`, and check the fingerprints), or ssh to each host by
  hand one time.

### Step 2. Make sure each person may `sudo su - oracle` (root)

The service uses each person's own sudo rights, so anyone who can already do step 1 is ready. Your
rule is passwordless, for example:

```
%dba ALL=(root) NOPASSWD: /bin/su - oracle*, /usr/bin/su - oracle*
```

(A rule without `NOPASSWD:` also works: the service then answers sudo with the person's password.)
If `/etc/sudoers` contains `Defaults requiretty`, add `Defaults:%dba !requiretty`, because the
service's commands have no terminal.

### Step 3. Allow password logins to sshd from the jump server itself (root)

The service logs people in over SSH to `127.0.0.1`. Check that password (or keyboard-interactive)
login works locally:

```bash
ssh -o PreferredAuthentications=password,keyboard-interactive jsmith@127.0.0.1 true && echo ok
```

If your sshd has `PasswordAuthentication no` and keyboard-interactive is also off, allow it for
localhost only by adding this at the **end** of `/etc/ssh/sshd_config`:

```
Match Address 127.0.0.1,::1
    PasswordAuthentication yes
```

Then `sshd -t && systemctl reload sshd`.

### Step 4. Create the service account (root)

```bash
useradd -r -m -d /opt/dbaops-api -s /sbin/nologin dbaops
mkdir -p /etc/dbaops-api /var/log/dbaops-api
chown dbaops:dbaops /var/log/dbaops-api
```

`dbaops` needs no sudo rule and no SSH key. It only runs the web service.

### Step 5. Copy the code and record the jump server's own host key (root)

```bash
cp -r jumpserver-flask-api/. /opt/dbaops-api/
ssh-keyscan -t rsa,ecdsa,ed25519 127.0.0.1 > /opt/dbaops-api/known_hosts
chown -R dbaops:dbaops /opt/dbaops-api
```

The service refuses to send passwords to an SSH server whose key is not in that file.

### Step 6. Create a virtual environment with Python 3.8 (root)

```bash
cd /opt/dbaops-api
sudo -u dbaops /usr/local/bin/python3.8 -m venv venv
sudo -u dbaops venv/bin/pip install --upgrade pip
sudo -u dbaops venv/bin/pip install -r requirements.txt   # Flask 3.0.3, gunicorn 23.0.0, paramiko 3.5.1
sudo -u dbaops venv/bin/python -W ignore -c "import flask, gunicorn, paramiko; print('ok')"
```

paramiko needs `cryptography`, `bcrypt` and `pynacl`. pip installs prebuilt wheels for them that run
on RHEL 7, so no compiler is needed. A `CryptographyDeprecationWarning` about Python 3.8 is expected
and harmless.

**If the jump server has no internet access**: on any Linux machine with internet, download wheels
that match RHEL 7 and Python 3.8, copy `wheels/` over, and install from it:

```bash
pip download -r requirements.txt -d wheels --only-binary=:all: \
    --python-version 3.8 --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64
# on the jump server:
sudo -u dbaops venv/bin/pip install --no-index --find-links wheels -r requirements.txt
```

### Step 7. Configure

```bash
cp config.example.json config.json && chown dbaops:dbaops config.json
vi config.json
```

The `jumpserver` section controls logins:

| key | meaning |
| --- | --- |
| `host`, `port` | Where people log in. Keep `127.0.0.1:22`, the jump server itself. |
| `known_hosts` | Host keys for that address (step 5). |
| `allowed_users` | Optional list. When it is not empty, only these usernames may log in to the service. |
| `allow_http_login` | Leave `false`. Passwords are only accepted over HTTPS or from the jump server itself. |
| `max_login_failures`, `login_failure_window` | Failed logins allowed per username or IP before `429` (default 5 in 600 s). |
| `session_idle_seconds` | Log out after this much inactivity (default 1800). |
| `sudo_prompt_wait` | Seconds to wait for sudo's password prompt before closing input (default 20). |

The rest:

| key | meaning |
| --- | --- |
| `script_dir` | `/dba/bin`. Every script runs as `<script_dir>/<file>` on the target. |
| `become` | How to become oracle. Default `["sudo","su","-","oracle","-c"]`. The service adds `-S -p <prompt>` after `sudo`. |
| `ccps` | The real ccps command, run in the oracle shell before ssh. |
| `ccps_passphrase_env` | Environment variable holding the cp993 passphrase (default `CCPS_PASSPHRASE`). |
| `ssh_user` | `null` connects as oracle (the current user). Set a name for `user@host`. |
| `ssh_options` | Extra `ssh` options. Keep `BatchMode=yes` so ssh never waits for a password. |
| `host_pattern` | Regex every `host` must match. Tighten it to your naming standard, for example `^db[a-z0-9-]+(\\.corp\\.example\\.com)?$`. |
| `allowed_hosts` | Optional exact list of DB servers. |
| `scripts` | The allow-list (see below). |
| `default_timeout` | Seconds a script may run (default 600). Can be overridden per script. |
| `max_concurrent` | Scripts running at the same time across all people. Extra requests get `429`. Keep it at 10 or below, because sshd allows 10 channels per login by default (`MaxSessions`). |
| `session_ttl_seconds` | Maximum session length (default 8 hours). |
| `audit_log` | JSON-lines file recording every login, logout, and run, with the person's username. |
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
Keys that start with `_` are ignored, so you can use `"_comment"`.

### Step 8. Store the ccps passphrase, then test as yourself (root)

Create the env file and put the cp993 passphrase in it:

```bash
cp /opt/dbaops-api/deploy/dbaops.env.example /etc/dbaops-api/dbaops.env
chown root:dbaops /etc/dbaops-api/dbaops.env && chmod 640 /etc/dbaops-api/dbaops.env
vi /etc/dbaops-api/dbaops.env          # set:  CCPS_PASSPHRASE=<the cp993 passphrase>
```

The CLI and the service both read this file. Then:

```bash
cd /opt/dbaops-api
sudo -u dbaops venv/bin/python -m dbaops_api check      # should say "ccps passphrase (CCPS_PASSPHRASE): set"
sudo -u dbaops venv/bin/python -m dbaops_api dry-run --host dbprod01 --script dba_checkout
sudo -u dbaops venv/bin/python -W ignore -m dbaops_api try --user jsmith --host dbprod01 --script dba_checkout
```

`try` asks for jsmith's password, logs in over SSH exactly as the API will, runs the script, and prints
the result. Fix any error here before going on (see Troubleshooting).

### Step 9. Create a TLS certificate (root)

People send their password to the service, so it must use HTTPS. Use a certificate from your
internal CA if you have one. Otherwise create a self-signed one:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$(hostname -f)" \
    -keyout /etc/dbaops-api/tls.key -out /etc/dbaops-api/tls.crt
chown root:dbaops /etc/dbaops-api/tls.key /etc/dbaops-api/tls.crt
chmod 640 /etc/dbaops-api/tls.key
```

RHEL 7's OpenSSL 1.0.2 does not support `-addext`. If the harness checks the hostname, add a
`subjectAltName` through a small `-config` file. Copy `tls.crt` to the harness host so it can trust it.

### Step 10. Run it as a systemd service (root)

The unit loads `/etc/dbaops-api/dbaops.env` from step 8, including `CCPS_PASSPHRASE`.

```bash
cp /opt/dbaops-api/deploy/dbaops-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dbaops-api
systemctl status dbaops-api
journalctl -u dbaops-api -f
curl -sk https://127.0.0.1:8443/health
```

The unit runs gunicorn with **exactly one worker** (`--workers 1 --threads 16`). Logins live in that
process's memory, so a second worker would not know about them. `--timeout 960` must be longer than
your longest script timeout plus 30 seconds.

### Step 11. Open the port only to the agent harness host (root)

```bash
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.20.30.40/32" port port="8443" protocol="tcp" accept'
firewall-cmd --reload
```

Replace `10.20.30.40` with the harness host's IP. SELinux needs no change for gunicorn started by
systemd.

### Step 12. Call it from the agent harness host

```bash
export JUMP=https://jumpserver.example.com:8443
export CA=/path/to/tls.crt          # the certificate from step 9

# 1. log in as yourself (read -s keeps the password off the screen and out of shell history)
read -s -p "jump server password: " PW; echo
TOKEN=$(curl -s --cacert $CA -X POST $JUMP/session -H 'Content-Type: application/json' \
          -d "{\"username\":\"$USER\",\"password\":\"$PW\"}" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
unset PW

# 2. run scripts with the token
curl -s --cacert $CA -X POST $JUMP/run \
  -H "X-Session-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"host":"dbprod01","script":"dba_checkout","confirm":false,"analyze":false}'

# 3. log out when done
curl -s --cacert $CA -X DELETE $JUMP/session -H "X-Session-Token: $TOKEN"
```

A password that contains `"` or `\` breaks the hand-written JSON above. The Python client below
handles any password.

---

## API reference

All endpoints except `/health` and `POST /session` need `X-Session-Token: <token>` (or
`Authorization: Bearer <token>`).

| method | path | purpose |
| --- | --- | --- |
| GET | `/health` | Liveness check, no token. |
| POST | `/session` | Body `{"username", "password"}` (the person's jump server login). Returns `{"token", "user", "expires_at", "idle_expires_at", "idle_timeout"}`. |
| GET | `/session` | Who this token belongs to and when it expires. |
| DELETE | `/session` | Log out and close the SSH login. |
| GET | `/scripts` | The allow-listed scripts, with path, args, `requires_confirm`, timeout. |
| GET | `/tools` | The same list as an OpenAI-style function tool (`run_dba_script`) for your LLM. |
| POST | `/run` | Run one script on one host as the logged-in person. |

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
  "user": "jsmith",
  "duration_s": 12.4
}
```

A script that runs but exits non-zero still returns `200`, with that `exit_code` and
`"status": "script_failed"`, so your agent sees the output (check scripts often exit non-zero on warnings).

Errors (always JSON with an `error` field):

| code | when |
| --- | --- |
| `400` | Bad host, unknown script, body is not JSON, `confirm`/`analyze` not a boolean, `analyze: true` with no LLM configured, or (on login) a missing or invalid username or password field. |
| `401` | Missing, unknown, or expired session token, or (on login) a wrong username or password. |
| `403` | Login attempted over plain HTTP from another host. |
| `409` | State-changing script without `"confirm": true`. |
| `429` | Too many failed logins, or `max_concurrent` scripts already running. |
| `502` | Jump server problem. `status` says which: `become_failed` (sudo refused, for example "not in the sudoers file"), `ccps_failed` (ssh-agent/ssh-add), `ssh_failed` (ssh exit 255: unreachable, host key, auth), `timeout`, `jump_failed` (the SSH login dropped). On login, `502` means sshd could not be reached or its host key is unknown. |

### Using it from your agent

`examples/agent_client.py` is a standard-library client for the harness host. Each person gives
their own username and password. It logs in again automatically on `401` (for example after an idle
timeout or a service restart):

```python
import getpass
from agent_client import DbaOpsClient, handle_tool_call

client = DbaOpsClient("https://jumpserver:8443", username="jsmith",
                      password=getpass.getpass("jump server password: "),
                      cafile="/path/to/tls.crt")
tools = client.tools()                 # hand these to your LLM as its tool list
result = client.run("dbprod01", "dba_checkout")
print(result["user"], result["exit_code"], result["output"])

# when the LLM calls run_dba_script, pass its arguments string through:
tool_output = handle_tool_call(client, tool_call.function.arguments)

client.logout()
```

From the shell: `python3 agent_client.py --user jsmith dbprod01 dba_checkout` asks for the password.

Keep a person in the loop for state-changing scripts: have the harness ask for approval before it
sends `"confirm": true`, rather than letting the LLM set it on its own.

---

## Security summary

- Every command runs in the person's own SSH login, with their own sudo rights. The service account
  (`dbaops`) has no sudo rights and no keys.
- Passwords are accepted only over HTTPS (or from the jump server itself). They are never logged or
  written to disk, they are masked in returned output, and they are sent only to an SSH server whose
  host key is in `known_hosts`.
- The cp993 passphrase lives only in `/etc/dbaops-api/dbaops.env` (mode 640, root:dbaops). It reaches
  `ssh-add` through stdin and a one-time askpass helper. It is never on a command line, never logged,
  never passed to the target, and masked in output.
- Failed logins are rate-limited per username and per IP, on top of the jump server's own PAM lockout.
- Sessions expire after inactivity and after a maximum lifetime, and `DELETE /session` closes them
  at once. Restarting the service logs everyone out.
- Only allow-listed script names run. Script `file` values cannot contain `/`, so nothing outside
  `/dba/bin` can be reached. `host` must match `host_pattern`, which cannot start with `-`, so it can
  never be read as an ssh option. Every value is shell-quoted.
- The audit log records every login (ok, failed, or rate-limited), logout, and run with the username,
  source IP, host, script, status, and exit code.

## Troubleshooting

| symptom | cause and fix |
| --- | --- |
| Login `401` with the right password | sshd refuses password login from 127.0.0.1 (step 3), the account is locked (`faillock --user jsmith`), or the password has expired. Try `ssh jsmith@127.0.0.1` by hand. |
| Login `502`, `host key ... is not in .../known_hosts` | Run the `ssh-keyscan` command from step 5. |
| Login `403`, `Refusing to accept a password over plain HTTP` | Call the service over `https://`. |
| `502 become_failed`, `is not in the sudoers file` / `is not allowed to execute` | That person has no `sudo su - oracle` rule (step 2). |
| `502 become_failed`, `incorrect password attempt` | sudo rejected the login password. This happens when sudo authenticates against something other than the login password. |
| `502 become_failed`, `you must have a tty to run sudo` | Add `Defaults:%dba !requiretty` (step 2). |
| `502 ccps_failed`, `Could not open a connection to your authentication agent` | `ssh-agent` did not start. Check the `ccps` value in `config.json`. |
| `502 ccps_failed`, `No such file` or `bad permissions` | `~oracle/.ssh/cp993` is missing or not mode 600. |
| `502 ccps_failed`, `check CCPS_PASSPHRASE` | The passphrase in `/etc/dbaops-api/dbaops.env` is wrong. Test it as oracle with `ssh-keygen -y -f ~/.ssh/cp993`, fix the file, then `systemctl restart dbaops-api`. |
| `502 ccps_failed`, `set CCPS_PASSPHRASE` | cp993 has a passphrase but `CCPS_PASSPHRASE` is empty (step 8). |
| `502 ssh_failed`, `Host key verification failed` | Add the target to `~oracle/.ssh/known_hosts` (step 1). |
| `502 ssh_failed`, `Permission denied (publickey)` | cp993's public key is not in the target's `authorized_keys`. |
| `502 timeout` | The script ran longer than its `timeout`. Raise it for that script, and keep gunicorn's `--timeout` higher. |
| `401 Unknown or expired session` right after logging in | gunicorn is running more than one worker. Use `--workers 1` as in the unit file. |
| `output` contains banner text | The banner is printed by the *target's* login, not the jump server's. Remove it there or filter it in your agent. |
| `pip install` fails with `SSL module is not available` | Python 3.8 was built without `openssl-devel`. Rebuild it, or use the offline wheel install in step 6. |

## Tests

```bash
venv/bin/pip install pytest
venv/bin/python -m pytest -q
```

`tests/test_shell.py` runs the generated oracle shell for real, with `ssh-agent`, `ssh-add`, and
`timeout`, against a stub `ssh`. `tests/test_pump.py` checks that the password is sent only when
sudo's prompt appears and that input is closed afterwards.
