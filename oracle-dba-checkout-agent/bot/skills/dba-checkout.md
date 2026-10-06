---
description: Use when the user wants the Oracle DBA checkout log from a target server reached through pretzel.
---

# DBA checkout

1. If the user supplies a pretzel account and password, call `set_pretzel_login` first. Confirm the account name only.
2. Confirm the user named a target server. If they did not, ask for the hostname.
3. Call `run_dba_checkout` with `confirm` true and that `targetHost`. Do not pass the login or password to this tool.
4. The tool SSHes to pretzel.int.thomsonreuters.com, runs `sudo su - oracle`, answers `ccps`, then runs `/dba/bin/dba_checkout` on the target.
5. Reply with the exit status, jump host, target host, and `downloadPath` on its own line.
6. Summarize `preview` in a few sentences. The full log is the download.
