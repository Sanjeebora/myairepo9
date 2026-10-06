---
description: Use when the user wants the Oracle DBA checkout log from a target server reached through pretzel.
---

# DBA checkout

1. Confirm the user named a target server. If they did not, ask for the hostname.
2. Call `run_dba_checkout` with `confirm` true and that `targetHost`. Do not pass a password, jump host, or command.
3. The tool SSHes to pretzel.int.thomsonreuters.com, runs `sudo su - oracle`, answers `ccps`, then runs `/dba/bin/dba_checkout` on the target.
4. Reply with the exit status, jump host, target host, and `downloadPath` on its own line.
5. Summarize `preview` in a few sentences. The full log is the download.
