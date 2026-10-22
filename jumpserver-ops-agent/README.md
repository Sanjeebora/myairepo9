# Jumpserver Ops Agent

Connects to a jumpserver, runs `ccps` to enable passwordless SSH, runs predefined
scripts (such as `dba_checkout`) on your servers, then sends the output to your LLM
and gets back a prioritized action plan.

```
this machine --SSH--> jumpserver --(ccps)--> ssh target --> dba_checkout.sh
                                                              |
       report.md / report.json  <--  LLM action plan  <-- script output (redacted)
```

## Safety

- The agent runs only scripts listed under `scripts:` in `config.yaml`. The LLM never chooses or runs commands.
  It only reads output, and any commands it suggests are written to the report for a person to review.
- Passwords, `IDENTIFIED BY ...`, `user/pass@tns` strings, and private keys are masked before output
  reaches the LLM or is saved to disk. Add your own regexes in `redact_patterns`.
- Jumpserver host keys are verified against `known_hosts` by default.
- Secrets (the LLM API key and the jumpserver password) are read from environment variables or `.env`, never from the config file.

## Setup on Windows (PowerShell)

Run each line on its own. Windows PowerShell 5.1 does not accept `&&`, and the virtual environment lives in `.venv\Scripts`, not `.venv/bin`.

```powershell
cd jumpserver-ops-agent
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
notepad config.yaml
notepad .env
python -m ops_agent web
```

Then open http://127.0.0.1:5000 in your browser. Tick `dba_checkout`, tick **Dry run** the first time, and press Run. A dry run only shows the commands. It does not connect.

If `python` is not found, use `py -3` instead of `python` on the `venv` line.

## Setup on Linux or macOS

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit jumpserver, servers, scripts, llm
cp .env.example .env                 # set JUMP_USER, LLM_API_KEY
python -m ops_agent web              # http://127.0.0.1:5000
```

### LLM settings

| Your LLM | Settings |
| --- | --- |
| OpenAI | `provider: openai`, `model: gpt-4o` |
| OpenAI-compatible gateway (vLLM, Ollama, LiteLLM, internal gateway) | `provider: openai`, `base_url: https://host/v1`, `model: <name>` |
| Azure OpenAI | `provider: azure`, `base_url: https://<resource>.openai.azure.com`, `api_version: 2024-06-01`, `model: <deployment>` |

If your endpoint rejects `response_format`, set `json_mode: false`.

### ccps modes

- `mode: once` (default): runs `ccps` once after connecting. It must exit with code 0.
- `mode: per_command`: runs `ccps` in the same shell before every `ssh` to a target. Use this if
  `ccps` only affects the current shell session, for example by starting an `ssh-agent`.

`ccps` must not prompt for input. If it asks for a password or OTP, tell me how it prompts so that support can be added.

### Scripts

- `remote_path`: the script already exists on each target and is run as `ssh target /path/script args`.
- `local_file`: the script is stored in this project and sent to each target over stdin (`bash -s`), so you do not need to copy it to the servers.

## Web UI

`python -m ops_agent web` starts Flask on http://127.0.0.1:5000 and listens on this computer only. The page lists the scripts and servers from `config.yaml`. There is no box for a free-form command: the agent can run only the allowlisted scripts.

- **Dry run** prints the `ccps` and `ssh` commands and does not connect.
- **Skip the LLM** collects script output and skips the action plan.
- Finished runs are listed under **Reports**, including the redacted raw logs.

## Command line

```bash
python -m ops_agent list                                     # validate config
python -m ops_agent run -s dba_checkout --dry-run            # show exact commands, connect to nothing
python -m ops_agent run -s dba_checkout                      # all servers
python -m ops_agent run -s dba_checkout --tags prod
python -m ops_agent run -s dba_checkout -s os_checkout --servers proddb01,proddb02
python -m ops_agent run -s dba_checkout --no-llm             # collect output only
python -m ops_agent web                                      # browser UI
```

Each run writes `reports/<timestamp>/`:

- `report.md`: summary, per-server findings, and the prioritized action plan
- `report.json`: machine-readable plan and run metadata
- `raw/<server>__<script>.log`: redacted script output

Exit codes: `0` means all scripts succeeded, `2` means a script failed, timed out, or could not connect, and `1` means a config or connection error. These codes let you use the agent in cron or CI.

The LLM's instructions are in `ops_agent/prompts/system.md`. Edit that file to change the
severity rules or the output format.

## Tests

```bash
python -m pytest -q
```
