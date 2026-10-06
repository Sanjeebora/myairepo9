# Oracle DBA checkout agent

On-demand chat agent that SSHes to `pretzel.int.thomsonreuters.com`, runs `sudo su - oracle`, answers the `ccps` password prompt, then runs `/dba/bin/dba_checkout` on the target server and stores the output as a downloadable log.

The model cannot choose the jump host or the commands. It can pass the target hostname, and it can save a pretzel account and password that the user supplies.

## Sequence

1. SSH to `pretzel.int.thomsonreuters.com`.
2. Run `sudo su - oracle`.
3. Run `ccps` and answer its password prompt.
4. From the oracle account, SSH to the named target without a password.
5. Run `/dba/bin/dba_checkout` and save the output.

`sudo` on pretzel must not ask for a password. The ccps password is not sent to sudo.

## Configure

Set these on the host before a live checkout. Do not put them in chat or in git.

| Variable | Required | Meaning |
| --- | --- | --- |
| `ORACLE_DBA_SSH_USER` | unless saved | Account on pretzel that can `sudo su - oracle` |
| `ORACLE_DBA_SSH_PASSWORD` | unless saved | Password for that pretzel account |
| `ORACLE_DBA_SSH_KNOWN_HOSTS` | yes | Host key line for `pretzel.int.thomsonreuters.com` |
| `ORACLE_DBA_CCPS_PASSWORD` | yes | Password `ccps` asks for |
| `ORACLE_DBA_SSH_PORT` | no | Default `22` |
| `ORACLE_DBA_CHECKOUT_TIMEOUT_SECONDS` | no | Default `900`, range `30`–`1800` |

To supply the pretzel account from chat, the agent calls `set_pretzel_login`. That saves the account and password in agent storage and replaces any previous saved login. A saved login is used instead of `ORACLE_DBA_SSH_USER` and `ORACLE_DBA_SSH_PASSWORD`. The password is not returned in the tool result or written to git.

A gitignored `.env.local` is loaded for any of these that are not already in the environment.

Capture the pretzel host key from a trusted network:

```bash
ssh-keyscan -p 22 -T 5 pretzel.int.thomsonreuters.com
```

Hosted deploy egress is limited to `pretzel.int.thomsonreuters.com:22`. The second hop to the target starts on pretzel, not on the agent.

## Run

```bash
npx bdk dev
```

Ask it to run the checkout on a target, for example `dbhost.example.com`. The reply includes `/v1/artifacts/<id>/content`. Download the log with:

```bash
curl -fsS "http://127.0.0.1:3000/v1/artifacts/<id>/content" -o dba-checkout.log
```

Direct tool call:

```bash
npx bdk call run_dba_checkout --dir . --input '{"confirm":true,"targetHost":"dbhost.example.com"}'
```

## Limits

- Jump host and commands are fixed. The only variable is the target hostname.
- `/dba/bin/dba_checkout` is run with no arguments.
- Passwords are redacted from the stored log.
- A second checkout waits until the in-flight one finishes or its lock expires.
- Eval sessions do not open SSH.
