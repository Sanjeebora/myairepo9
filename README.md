# myairepo9
AI generated automation and agents

- [`jumpserver-ops-agent/`](jumpserver-ops-agent/): runs predefined scripts through a jump server and asks an LLM for an action plan.
- [`oracle-dba-checkout-agent/`](oracle-dba-checkout-agent/): BDK agent that runs `dba_checkout` through the jump server.
- [`oracle-jumpserver-flask-tool/`](oracle-jumpserver-flask-tool/): local Flask API (Windows-friendly) that an LLM agent calls as a tool to run allowlisted Oracle scripts via the jump server.
