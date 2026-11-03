# RHEL 7 / BIG-IP deployment

The Flask service is installed on actual jump server `10.17.66.19` and binds
`0.0.0.0:8080`. BIG-IP maps `10.17.34.78:80` to that pool member. BIG-IP's
separate `10.17.34.78:22 -> 10.17.66.19:22` mapping is for human SSH access;
the Flask process does not use it.

1. Install the approved CPython 3.6.4 under `/opt/python-3.6.4`, create
   `/opt/dbaops-api/venv`, pin pip to 21.3.1, and install `requirements.txt`.
2. Create the unprivileged `dbaops` service account. Install application files
   root-owned under `/opt/dbaops-api`; only `/var/log/dbaops-api` should be
   writable by `dbaops`.
3. Grant `dbaops` the narrowly administered, passwordless ability needed to
   run `sudo -n su - oracle -c ...`. Disable `requiretty` only for this account.
   Review this privilege with the host security owner.
4. As oracle, verify `ccps` accepts its secret on standard input, then verify
   passwordless SSH and each remote `/dba/bin` operation manually.
5. Build `/etc/dbaops-api/target_known_hosts` from target keys obtained through
   a trusted source. Verify fingerprints out of band. Make it `root:oracle`
   mode 0640.
6. Copy `config.example.json` to `/etc/dbaops-api/config.json`, replace the
   example target hosts/CIDRs with the narrowest approved policy, and keep
   target ports restricted to 22 unless a specific exception is approved.
7. Copy `dbaops.env.example` to `/etc/dbaops-api/dbaops.env` as root mode 0600.
   Set a random `DBAOPS_API_TOKEN` of at least 32 characters and `CCPS_SECRET`.
8. Copy `dbaops-api.service` to `/etc/systemd/system`, then run:

   ```bash
   systemctl daemon-reload
   systemctl enable --now dbaops-api
   curl --fail http://127.0.0.1:8080/health
   ```

9. Configure the BIG-IP HTTP virtual server:

   ```text
   virtual address: 10.17.34.78
   service port:    80
   pool member:     10.17.66.19:8080
   health monitor:  GET /health
   ```

10. Restrict host TCP/8080 ingress to BIG-IP self/SNAT addresses and approved
    administration sources. Restrict outbound TCP to policy-approved target
    CIDRs and ports.

## TLS warning

VIP port 80 is plaintext. `X-Token` is exposed to any party able to observe or
modify that network path. The sample's `allow_insecure_http: true` is an
explicit compatibility switch for the clarified mapping, not equivalent to
transport security.

Preferred production state:

1. add BIG-IP VIP port 443 with an approved client-side TLS certificate/profile;
2. keep the server-side pool mapping to `10.17.66.19:8080`;
3. redirect or remove VIP port 80; and
4. set `allow_insecure_http` to `false`.

Test an allowlisted target, a denied hostname, a denied port, a hostname that
resolves outside allowed CIDRs, an unknown host key, a state-changing request
without confirmation, and timeout cleanup. Forward the metadata-only audit log
to the approved SIEM.
