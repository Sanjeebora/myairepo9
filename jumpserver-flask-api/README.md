# DBA Jump-Server Gateway

Production-oriented Flask gateway for RHEL 7.9, CPython 3.6.4, and pip 21.3.1.
It runs on the actual jump server `10.17.66.19:8080`. BIG-IP publishes
`10.17.34.78:80` to that HTTP service and independently maps VIP port 22 to the
actual jump server's port 22.

The Flask service never SSHes to either jump-server address. For each authorized
request it:

1. validates `X-Token`;
2. validates the requested target host and port against exact-host, CIDR, and
   port policy;
3. locally runs `sudo -n su - oracle`;
4. feeds the server-side `CCPS_SECRET` to local interactive `ccps`; and
5. uses the resulting passwordless credentials to SSH from the jump server to
   the validated target and execute one fixed `/dba/bin` script.

Allowed scripts are `dba_checkout`, `start_oracle`, `stop_oracle`, and
`maint_mode.sh`. State-changing operations require `confirm: true`. There is no
free-form command, argument, path, SSH user, or target password field.

## API

```http
POST /run HTTP/1.1
Host: 10.17.34.78
X-Token: <gateway token>
Content-Type: application/json

{"target_host":"dbprod01.example.internal","target_port":22,
 "script":"dba_checkout","confirm":false}
```

Example response:

```json
{
  "jump_host": "10.17.66.19",
  "target_host": "dbprod01.example.internal",
  "target_port": 22,
  "script": "dba_checkout",
  "command": "ssh ... dbprod01.example.internal /dba/bin/dba_checkout",
  "exit_code": 0,
  "output": "...",
  "stderr": "",
  "status": "ok",
  "duration_s": 4.2
}
```

Script nonzero exits return HTTP 200 with `status=script_failed`.
Infrastructure failures return HTTP 502. Invalid policy/input returns 400,
missing confirmation returns 409, and saturation returns 429.

## Target policy

`config.json` must define at least one `target_allowed_hosts` or
`target_allowed_cidrs` entry and a non-empty `target_allowed_ports` list.
Every hostname is resolved during validation and SSH receives the validated
numeric address to prevent DNS rebinding between policy evaluation and
connection. Exact hostname entries use OpenSSH `HostKeyAlias` so keys remain
pinned to the configured name. Both jump-server addresses are rejected as
targets regardless of allowlist or DNS.

OpenSSH uses `BatchMode=yes`, strict host-key checking, a dedicated
`target_known_hosts` file, and bounded connection/operation deadlines. Output
is continuously drained, retained to a configured cap, UTF-8 decoded safely,
and redacted for the ccps secret.

## BIG-IP and TLS

The clarified VIP mapping `10.17.34.78:80 -> 10.17.66.19:8080` is plaintext.
It does not protect `X-Token` from observation or modification. The sample
configuration enables `allow_insecure_http` only so this exact mapping can
operate on a trusted, source-restricted network.

The production recommendation is to add a BIG-IP HTTPS virtual server (normally
VIP port 443) with an approved client-side TLS profile, forward to pool member
`10.17.66.19:8080`, and then set `allow_insecure_http` to `false`. In either
case, restrict the pool member's port 8080 so only BIG-IP health checks/data
plane addresses and approved local administrators can reach it.

## Deployment and testing

See [`deploy/README.md`](deploy/README.md).

```bash
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
DBAOPS_API_TOKEN=test-token-with-at-least-32-characters CCPS_SECRET=test-secret \
  venv/bin/python -m pytest -q
```

Validate configuration without displaying secret values:

```bash
DBAOPS_API_TOKEN='from-secret-store' CCPS_SECRET='from-secret-store' \
  venv/bin/python -m dbaops_api -c /etc/dbaops-api/config.json check
```
