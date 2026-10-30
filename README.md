# myairepo9
AI generated automation and agents

- `jumpserver-flask-api/`: Flask JSON API that runs on the jump server. People log in with their own jump server username and password; an agent harness then uses that session to run allow-listed `/dba/bin` scripts on DB servers through `sudo su - oracle` and `ccps`.
- `jumpserver-ops-agent/`: laptop-side CLI and web UI that connects to the jump server over SSH and sends script output to an LLM.
- `oracle-dba-checkout-agent/`: Agent SDK agent for DBA checkouts.
