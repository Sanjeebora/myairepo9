# RHEL 7 deployment

1. Give the gateway host a routed corporate network path, VPN, or approved
   host-level tunnel to private address `10.17.34.78`; verify TCP/22 is
   reachable.
2. Install the organization's approved CPython 3.6.4 under
   `/opt/python-3.6.4`. Create `/opt/dbaops-api/venv`, pin pip to 21.3.1, and
   install `requirements.txt`.
3. Create the `dbaops` system account. Install the application in
   `/opt/dbaops-api`, `/etc/dbaops-api` as `root:dbaops` mode 0750, and
   `/var/log/dbaops-api` as `dbaops:dbaops` mode 0750.
4. Verify the SSH fingerprint for `10.17.34.78` out of band, then save its
   public key in `/opt/dbaops-api/known_hosts`, owned by `dbaops`, mode 0600.
5. Copy `config.example.json` to `/etc/dbaops-api/config.json` as
   `root:dbaops`, mode 0640.
6. Copy `dbaops.env.example` to `/etc/dbaops-api/dbaops.env` as root, mode 0600.
   Set random `DBAOPS_API_TOKEN` (at least 32 characters) and `CCPS_SECRET`
   values. Never commit this file.
7. Copy `dbaops-api.service` to `/etc/systemd/system`, run
   `systemctl daemon-reload`, then `systemctl enable --now dbaops-api`.
8. Obtain an internal-CA TLS certificate. Copy `nginx-dbaops.conf` into
   `/etc/nginx/conf.d`, adjust its DNS and certificate paths, run `nginx -t`,
   and enable nginx. Gunicorn remains loopback-only on port 8080; clients use
   HTTPS port 8443.
9. Restrict TCP/8443 ingress to harness addresses and TCP/22 egress to
   `10.17.34.78`. On enforcing SELinux hosts, enable nginx loopback proxying
   with `setsebool -P httpd_can_network_connect 1`.
10. Test `/health`, then perform a controlled `dba_checkout`. Verify plain HTTP
    is rejected, arbitrary scripts return 400, and state-changing operations
    return 409 without `confirm: true`.

The gateway logs metadata only. Forward `/var/log/dbaops-api/audit.log` to the
approved SIEM and monitor TLS expiry, 401/429/502 rates, route availability, and
SSH host-key changes.
