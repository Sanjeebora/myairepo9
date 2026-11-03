# myairepo9
AI generated automation and agents

- `jumpserver-flask-api/`: RHEL 7 / Python 3.6 Flask JSON gateway on `10.17.66.19`, published through BIG-IP VIP `10.17.34.78`. It authenticates harness requests with `X-Token`, runs local `ccps` as oracle, and uses passwordless outbound SSH to run one of four fixed `/dba/bin` scripts on policy-approved targets.
- `jumpserver-ops-agent/`: laptop-side CLI and web UI that connects to the jump server over SSH and sends script output to an LLM.
- `oracle-dba-checkout-agent/`: Agent SDK agent for DBA checkouts.
