import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const CHECKOUT_SCRIPT = "/dba/bin/dba_checkout" as const;
export const JUMP_HOST = "pretzel.int.thomsonreuters.com" as const;

const DEFAULT_TIMEOUT_SECONDS = 900;
const MIN_TIMEOUT_SECONDS = 30;
const MAX_TIMEOUT_SECONDS = 1800;
const MAX_LOG_BYTES = 2_000_000;

const HOSTNAME =
  /^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$/;
const IPV4 =
  /^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$/;
const USER = /^[A-Za-z_][A-Za-z0-9._-]{0,31}$/;

export type SshConfig = {
  jumpHost: typeof JUMP_HOST;
  port: number;
  user: string;
  password: string;
  ccpsPassword: string;
  knownHosts: string;
  timeoutSeconds: number;
};

export type CheckoutRun = {
  exitCode: number | null;
  timedOut: boolean;
  startedAt: string;
  finishedAt: string;
  log: string;
  truncated: boolean;
  logBytes: number;
  targetHost: string;
};

type DialogIo = {
  write(chunk: string): void;
  onData(handler: (chunk: string) => void): void;
  onClose(handler: () => void): void;
};

type DialogResult = {
  exitCode: number | null;
  output: string;
  timedOut: boolean;
  truncated: boolean;
};

export const PRETZEL_LOGIN_KEY = "pretzel-login";

export type PretzelLogin = {
  user: string;
  password: string;
};

export function assertPretzelUser(user: string): string {
  const value = user.trim();
  if (!USER.test(value)) {
    throw new Error("Pretzel login account must be a Unix account name.");
  }
  return value;
}

export function parsePretzelLogin(value: unknown): PretzelLogin | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const record = value as { user?: unknown; password?: unknown };
  if (typeof record.user !== "string" || typeof record.password !== "string") return undefined;
  if (record.password.length === 0 || record.password.length > 256) return undefined;
  return { user: assertPretzelUser(record.user), password: record.password };
}

export function assertTargetHost(host: string): string {
  const value = host.trim();
  if (!HOSTNAME.test(value) && !IPV4.test(value)) {
    throw new Error("targetHost must be a hostname or IPv4 address.");
  }
  if (value.toLowerCase() === JUMP_HOST) {
    throw new Error("targetHost must be the database server, not pretzel.");
  }
  return value;
}

export async function loadSshConfig(
  env: NodeJS.ProcessEnv,
  saved?: PretzelLogin,
): Promise<SshConfig> {
  const merged = await mergedEnv(env);
  const knownHosts = required(merged, "ORACLE_DBA_SSH_KNOWN_HOSTS");
  assertKnownHosts(JUMP_HOST, knownHosts);

  return {
    jumpHost: JUMP_HOST,
    port: parsePort(merged.ORACLE_DBA_SSH_PORT),
    user: saved?.user ?? pretzelUser(merged),
    password: saved?.password ?? pretzelPassword(merged),
    ccpsPassword: required(merged, "ORACLE_DBA_CCPS_PASSWORD"),
    knownHosts: knownHosts.trim() + "\n",
    timeoutSeconds: parseTimeout(merged.ORACLE_DBA_CHECKOUT_TIMEOUT_SECONDS),
  };
}

export async function runRemoteCheckout(
  config: SshConfig,
  targetHost: string,
): Promise<CheckoutRun> {
  const target = assertTargetHost(targetHost);
  const startedAt = new Date().toISOString();
  const dir = await mkdtemp(join(tmpdir(), "dba-checkout-"));
  const askpassPath = join(dir, "askpass.sh");
  const knownHostsPath = join(dir, "known_hosts");

  try {
    await writeFile(askpassPath, "#!/bin/sh\nprintf '%s\\n' \"$ORACLE_DBA_SSH_PASSWORD\"\n", {
      mode: 0o700,
    });
    await writeFile(knownHostsPath, config.knownHosts, { mode: 0o600 });

    const args = [
      "-o", "PreferredAuthentications=password",
      "-o", "PubkeyAuthentication=no",
      "-o", "KbdInteractiveAuthentication=no",
      "-o", "NumberOfPasswordPrompts=1",
      "-o", "IdentitiesOnly=yes",
      "-o", "IdentityFile=/dev/null",
      "-o", "StrictHostKeyChecking=yes",
      "-o", `UserKnownHostsFile=${knownHostsPath}`,
      "-o", "GlobalKnownHostsFile=/dev/null",
      "-o", "ConnectTimeout=30",
      "-o", "ServerAliveInterval=15",
      "-o", "LogLevel=ERROR",
      "-p", String(config.port),
      "-tt",
      `${config.user}@${config.jumpHost}`,
    ];

    const captured = await spawnDialog(args, {
      timeoutMs: config.timeoutSeconds * 1000,
      env: {
        PATH: process.env.PATH ?? "/usr/bin:/bin",
        HOME: process.env.HOME ?? "/tmp",
        SSH_ASKPASS: askpassPath,
        SSH_ASKPASS_REQUIRE: "force",
        DISPLAY: "none",
        ORACLE_DBA_SSH_PASSWORD: config.password,
      },
      secrets: [config.password, config.ccpsPassword],
      targetHost: target,
      ccpsPassword: config.ccpsPassword,
    });

    const finishedAt = new Date().toISOString();
    const log = renderLog({
      config,
      targetHost: target,
      startedAt,
      finishedAt,
      exitCode: captured.exitCode,
      timedOut: captured.timedOut,
      truncated: captured.truncated,
      output: captured.output,
    });

    return {
      exitCode: captured.exitCode,
      timedOut: captured.timedOut,
      startedAt,
      finishedAt,
      log,
      truncated: captured.truncated,
      logBytes: Buffer.byteLength(log),
      targetHost: target,
    };
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

const TARGET_CHECKOUT_SCRIPT = [
  "#!/bin/bash",
  "set +e",
  "log=$(mktemp)",
  "trap 'rm -f \"$log\"' EXIT",
  CHECKOUT_SCRIPT + " >\"$log\" 2>&1",
  "checkout_rc=$?",
  "printf '%s\\n' '===== dba_checkout output ====='",
  "cat \"$log\"",
  "printf '%s\\n' \"===== dba_checkout exit: $checkout_rc =====\"",
  "db_down=0",
  "lsnr_down=0",
  "grep -Eiq 'database.{0,80}\\<down\\>|\\<down\\>.{0,80}database' \"$log\" && db_down=1",
  "grep -Eiq 'listener.{0,80}\\<down\\>|\\<down\\>.{0,80}listener' \"$log\" && lsnr_down=1",
  "if [ \"$db_down\" = 0 ] && [ \"$lsnr_down\" = 0 ]; then",
  "  printf '%s\\n' 'Database and listener are not reported down. No start commands were run.'",
  "  exit \"$checkout_rc\"",
  "fi",
  "printf '%s\\n' '===== recovery ====='",
  "fail=0",
  "if [ ! -r /etc/oratab ]; then",
  "  printf '%s\\n' '/etc/oratab is not readable.'",
  "  exit 1",
  "fi",
  "found=0",
  "while IFS= read -r line || [ -n \"$line\" ]; do",
  "  [ -z \"$line\" ] && continue",
  "  case \"$line\" in",
  "    [#]*) continue ;;",
  "  esac",
  "  sid=${line%%:*}",
  "  printf '%s\\n' \"$sid\" | grep -Eq '^[A-Za-z][A-Za-z0-9_]{0,29}$' || continue",
  "  found=1",
  "  printf '%s\\n' \"Instance name: $sid\"",
  "  printf '%s\\n' \"Running: start_oracle -i $sid\"",
  "  start_oracle -i \"$sid\"",
  "  rc=$?",
  "  printf '%s\\n' \"start_oracle exit: $rc\"",
  "  [ \"$rc\" -eq 0 ] || fail=1",
  "done < /etc/oratab",
  "if [ \"$found\" = 0 ]; then",
  "  printf '%s\\n' 'No instance name found in /etc/oratab.'",
  "  fail=1",
  "fi",
  "printf '%s\\n' 'Running: srvctl start listener'",
  "srvctl start listener",
  "rc=$?",
  "printf '%s\\n' \"srvctl exit: $rc\"",
  "[ \"$rc\" -eq 0 ] || fail=1",
  "if [ \"$fail\" -ne 0 ]; then",
  "  exit 1",
  "fi",
  "exit 0",
  "",
].join("\n");

export function targetCheckoutCommand(targetHost: string): string {
  const target = assertTargetHost(targetHost);
  const encoded = Buffer.from(TARGET_CHECKOUT_SCRIPT, "utf8").toString("base64");
  const remote = `echo ${encoded} | base64 -d | bash`;
  return `ssh -o BatchMode=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 -- ${target} bash -lc '${remote}'; printf '\\n__DBA_EXIT__:%s\\n' $?\n`;
}
