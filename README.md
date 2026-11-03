# myairepo9
AI generated automation and agents

- `jumpserver-flask-api/`: RHEL 7 / Python 3.6 compatible Flask JSON gateway. An agent harness authenticates with `X-Token`; the service SSHes to `10.17.34.78` with supplied user credentials, becomes oracle, authenticates `ccps`, and runs one of four fixed `/dba/bin` scripts.
- `jumpserver-ops-agent/`: laptop-side CLI and web UI that connects to the jump server over SSH and sends script output to an LLM.
- `oracle-dba-checkout-agent/`: Agent SDK agent for DBA checkouts.
