#!/usr/bin/env bash
# Read-only OS checks. Streamed to each target and run with `bash -s`.
echo "## host"; hostname; uptime
echo "## disk"; df -hP 2>/dev/null
echo "## memory"; free -m 2>/dev/null
echo "## load"; cat /proc/loadavg 2>/dev/null
echo "## failed services"; systemctl --failed --no-legend 2>/dev/null || echo "systemctl not available"
echo "## top cpu"; ps -eo pid,user,%cpu,%mem,comm --sort=-%cpu 2>/dev/null | head -n 6
