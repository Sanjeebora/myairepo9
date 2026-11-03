# DBA Jump-Server Gateway

Production-oriented Flask gateway for RHEL 7.9, CPython 3.6.4, and pip 21.3.1.
An agent harness authenticates with `X-Token`, supplies a person's SSH username
and password for `10.17.34.78`, and requests one fixed operation. The gateway
uses that login, runs `sudo su - oracle`, authenticates interactive `ccps` with
`CCPS_SECRET`, and executes exactly one allowlisted file under `/dba/bin`.

Allowed operations:

* `dba_checkout`
* `start_oracle` (requires `confirm: true`)
* `stop_oracle` (requires `confirm: true`)
* `maint_mode.sh` (requires `confirm: true`)

There is no command, argument, host, or path field. The jump host, script
directory, `ccps` command, and allowlist are enforced in code and validated at
startup.

## Request

```http
POST /run HTTP/1.1
X-Token: <gateway token>
Content-Type: application/json

{"username":"jsmith","password":"...","script":"dba_checkout","confirm":false}
```

Successful and script-failure responses are structured JSON:

```json
{
  "jump_host": "10.17.34.78",
  "script": "dba_checkout",
  "command": "/dba/bin/dba_checkout",
  "exit_code": 0,
  "output": "...",
  "stderr": "",
  "status": "ok",
  "user": "jsmith",
  "duration_s": 4.2
}
```

Infrastructure failures return JSON and HTTP 502. Invalid credentials return
401, invalid input returns 400, missing confirmation returns 409, and saturation
returns 429. Output is UTF-8 decoded with replacement, capped by
`max_output_bytes`, and redacted for the supplied SSH password and configured
ccps secret.

## Security properties

* `X-Token` uses constant-time comparison. The token and ccps secret are
  environment-only values and never accepted in request JSON.
* Paramiko rejects unknown SSH host keys. Password authentication cannot be
  redirected to another server.
* User input cannot become a command. Usernames are validated; script names map
  to four literal paths.
* Passwords are accepted only over TLS. A loopback reverse proxy is recognized
  only when the connection source is loopback and it sets
  `X-Forwarded-Proto: https`.
* The application logs only event metadata. It does not log headers, request
  bodies, command input, or raw script output.
* SSH and command operations have deadlines, concurrency is bounded, and the
  request body is limited to 16 KiB.

## Install and test

See [`deploy/README.md`](deploy/README.md) for deployment instructions. The
project delivery also includes a more detailed step-by-step operator guide.

For development on a supported modern host:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
DBAOPS_API_TOKEN=test-token-with-at-least-32-characters CCPS_SECRET=test-secret \
  venv/bin/python -m pytest -q
```

Validate production configuration:

```bash
DBAOPS_API_TOKEN='from-secret-store' CCPS_SECRET='from-secret-store' \
  venv/bin/python -m dbaops_api -c /etc/dbaops-api/config.json check
```

Production uses gunicorn bound to `127.0.0.1:8080` behind the supplied nginx TLS
configuration. Never expose Flask/gunicorn plaintext on the network. A private
`10.x` address is not Internet-routable: the service host must have a corporate
route, VPN, or approved tunnel to `10.17.34.78`.
