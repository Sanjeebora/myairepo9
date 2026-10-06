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

export async function loadSshConfig(env: NodeJS.ProcessEnv): Promise<SshConfig> {
  const merged = await mergedEnv(env);
  const user = required(merged, "ORACLE_DBA_SSH_USER");
  if (!USER.test(user)) {
    throw new Error("ORACLE_DBA_SSH_USER must be a Unix account name.");
  }

  const knownHosts = required(merged, "ORACLE_DBA_SSH_KNOWN_HOSTS");
  assertKnownHosts(JUMP_HOST, knownHosts);

  return {
    jumpHost: JUMP_HOST,
    port: parsePort(merged.ORACLE_DBA_SSH_PORT),
    user,
    password: required(merged, "ORACLE_DBA_SSH_PASSWORD"),
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

export function previewLog(log: string, limit = 4000): string {
  if (log.length <= limit) return log;
  return `${log.slice(0, limit)}\n…[preview truncated]`;
}

export async function driveCheckout(
  io: DialogIo,
  options: {
    targetHost: string;
    ccpsPassword: string;
    timeoutMs: number;
    secrets: string[];
  },
): Promise<DialogResult> {
  const target = assertTargetHost(options.targetHost);
  const deadline = Date.now() + options.timeoutMs;
  let output = "";
  let truncated = false;
  let closed = false;
  const waiters: Array<() => void> = [];

  const wake = () => {
    for (const waiter of [...waiters]) waiter();
  };

  io.onData((chunk) => {
    if (truncated) return;
    const next = Buffer.byteLength(output) + Buffer.byteLength(chunk);
    if (next > MAX_LOG_BYTES) {
      const room = Math.max(0, MAX_LOG_BYTES - Buffer.byteLength(output));
      output += chunk.slice(0, room);
      output += "\n…[log truncated]\n";
      truncated = true;
    } else {
      output += chunk;
    }
    wake();
  });
  io.onClose(() => {
    closed = true;
    wake();
  });

  let mark = 0;
  const since = () => output.slice(mark);

  const expect = (pattern: RegExp, label: string) =>
    new Promise<RegExpMatchArray>((resolve, reject) => {
      const check = () => {
        if (Date.now() >= deadline) {
          cleanup();
          reject(dialogError(`Timed out waiting for ${label}.`, output, options.secrets, true, truncated));
          return;
        }
        if (closed && !pattern.test(since())) {
          cleanup();
          reject(dialogError(`SSH session closed while waiting for ${label}.`, output, options.secrets, false, truncated));
          return;
        }
        const match = since().match(pattern);
        if (!match || match.index === undefined) return;
        mark += match.index + match[0].length;
        cleanup();
        resolve(match);
      };
      const timer = setInterval(check, 100);
      const cleanup = () => {
        clearInterval(timer);
        const index = waiters.indexOf(check);
        if (index >= 0) waiters.splice(index, 1);
      };
      waiters.push(check);
      check();
    });

  const quiet = (quietMs: number, label: string) =>
    new Promise<void>((resolve, reject) => {
      let lastChange = Date.now();
      let lastLen = output.length;
      const check = () => {
        if (output.length !== lastLen) {
          lastLen = output.length;
          lastChange = Date.now();
        }
        if (Date.now() >= deadline) {
          cleanup();
          reject(dialogError(`Timed out during ${label}.`, output, options.secrets, true, truncated));
          return;
        }
        if (closed) {
          cleanup();
          reject(dialogError(`SSH session closed during ${label}.`, output, options.secrets, false, truncated));
          return;
        }
        if (Date.now() - lastChange >= quietMs) {
          cleanup();
          resolve();
        }
      };
      const timer = setInterval(check, 100);
      const cleanup = () => {
        clearInterval(timer);
        const index = waiters.indexOf(check);
        if (index >= 0) waiters.splice(index, 1);
      };
      waiters.push(check);
    });

  try {
    io.write("printf '__JUMP_READY__\\n'\n");
    await expect(/(?:^|\n)__JUMP_READY__(?:\r?\n|$)/, "the pretzel shell");

    mark = output.length;
    io.write("sudo su - oracle\n");
    await quiet(1500, "sudo su - oracle");
    if (/\[sudo\] password|password for /i.test(since())) {
      throw dialogError(
        "sudo on pretzel asked for a password. The ccps password is only sent to ccps.",
        output,
        options.secrets,
        false,
        truncated,
      );
    }

    io.write("printf '__ORACLE_USER__:%s\\n' \"$(id -un)\"\n");
    const user = await expect(/__ORACLE_USER__:([A-Za-z0-9._-]+)/, "the oracle account");
    if (user[1] !== "oracle") {
      throw dialogError(
        `Expected the oracle account after sudo su - oracle, got ${user[1] ?? "an unknown account"}.`,
        output,
        options.secrets,
        false,
        truncated,
      );
    }

    mark = output.length;
    io.write("ccps\n");
    await expect(/password/i, "the ccps password prompt");
    io.write(`${options.ccpsPassword}\n`);
    await quiet(2000, "ccps");

    io.write("printf '__CCPS_DONE__:%s\\n' \"$(id -un)\"\n");
    const done = await expect(/__CCPS_DONE__:([A-Za-z0-9._-]+)/, "the shell after ccps");
    if (done[1] !== "oracle") {
      throw dialogError(
        "ccps did not return to the oracle shell.",
        output,
        options.secrets,
        false,
        truncated,
      );
    }

    io.write(
      `ssh -o BatchMode=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 -- ${target} ${CHECKOUT_SCRIPT}; printf '\\n__DBA_EXIT__:%s\\n' $?\n`,
    );
    const exit = await expect(/__DBA_EXIT__:(\d+)/, "the dba_checkout exit marker");
    io.write("exit\nexit\n");

    return {
      exitCode: Number(exit[1]),
      output: redact(output, options.secrets),
      timedOut: false,
      truncated,
    };
  } catch (error) {
    if (error instanceof DialogError) {
      return {
        exitCode: null,
        output: error.output,
        timedOut: error.timedOut,
        truncated: error.truncated,
      };
    }
    throw error;
  }
}

class DialogError extends Error {
  output: string;
  timedOut: boolean;
  truncated: boolean;

  constructor(message: string, output: string, timedOut: boolean, truncated: boolean) {
    super(message);
    this.output = output;
    this.timedOut = timedOut;
    this.truncated = truncated;
  }
}

function dialogError(
  message: string,
  output: string,
  secrets: string[],
  timedOut: boolean,
  truncated: boolean,
): DialogError {
  return new DialogError(message, `${redact(output, secrets)}\n${message}`, timedOut, truncated);
}

async function mergedEnv(env: NodeJS.ProcessEnv): Promise<NodeJS.ProcessEnv> {
  const merged: NodeJS.ProcessEnv = { ...env };
  const path = join(dirname(fileURLToPath(import.meta.url)), "../../.env.local");
  let text = "";
  try {
    text = await readFile(path, "utf8");
  } catch {
    return merged;
  }
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq <= 0) continue;
    const key = trimmed.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    if (merged[key]?.trim()) continue;
    merged[key] = trimmed.slice(eq + 1).trim();
  }
  return merged;
}

function required(env: NodeJS.ProcessEnv, name: string): string {
  const value = env[name]?.trim();
  if (!value) {
    throw new Error(`Missing required environment variable ${name}.`);
  }
  return value;
}

function parsePort(raw: string | undefined): number {
  if (raw === undefined || raw.trim() === "") return 22;
  if (!/^\d+$/.test(raw.trim())) {
    throw new Error("ORACLE_DBA_SSH_PORT must be an integer from 1 to 65535.");
  }
  const port = Number(raw.trim());
  if (port < 1 || port > 65535) {
    throw new Error("ORACLE_DBA_SSH_PORT must be an integer from 1 to 65535.");
  }
  return port;
}

function parseTimeout(raw: string | undefined): number {
  if (raw === undefined || raw.trim() === "") return DEFAULT_TIMEOUT_SECONDS;
  if (!/^\d+$/.test(raw.trim())) {
    throw new Error(
      `ORACLE_DBA_CHECKOUT_TIMEOUT_SECONDS must be an integer from ${MIN_TIMEOUT_SECONDS} to ${MAX_TIMEOUT_SECONDS}.`,
    );
  }
  const seconds = Number(raw.trim());
  if (seconds < MIN_TIMEOUT_SECONDS || seconds > MAX_TIMEOUT_SECONDS) {
    throw new Error(
      `ORACLE_DBA_CHECKOUT_TIMEOUT_SECONDS must be an integer from ${MIN_TIMEOUT_SECONDS} to ${MAX_TIMEOUT_SECONDS}.`,
    );
  }
  return seconds;
}

function assertKnownHosts(host: string, knownHosts: string): void {
  const lines = knownHosts
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#"));
  if (lines.length === 0) {
    throw new Error("ORACLE_DBA_SSH_KNOWN_HOSTS must contain the pretzel host key.");
  }
  const accepted = lines.some((line) => knownHostMatches(host, line));
  if (!accepted) {
    throw new Error(
      "ORACLE_DBA_SSH_KNOWN_HOSTS does not contain a key for pretzel.int.thomsonreuters.com.",
    );
  }
}

function knownHostMatches(host: string, line: string): boolean {
  const token = line.split(/\s+/, 1)[0] ?? "";
  if (token.startsWith("|1|")) return true;
  const names = token.split(",");
  return names.some((name) => {
    if (name === host) return true;
    if (name.startsWith("[") && name.endsWith("]")) {
      return name.slice(1, -1) === host;
    }
    const bracket = name.match(/^\[([^\]]+)\]:(\d+)$/);
    return bracket?.[1] === host;
  });
}

function renderLog(input: {
  config: SshConfig;
  targetHost: string;
  startedAt: string;
  finishedAt: string;
  exitCode: number | null;
  timedOut: boolean;
  truncated: boolean;
  output: string;
}): string {
  const header = [
    "# Oracle DBA checkout",
    `# jump: ${input.config.user}@${input.config.jumpHost}:${input.config.port}`,
    `# target: ${input.targetHost}`,
    `# script: ${CHECKOUT_SCRIPT}`,
    `# started: ${input.startedAt}`,
    `# finished: ${input.finishedAt}`,
    `# exit: ${input.exitCode === null ? "none" : String(input.exitCode)}`,
    `# timedOut: ${input.timedOut ? "true" : "false"}`,
    `# truncated: ${input.truncated ? "true" : "false"}`,
    "",
  ].join("\n");
  return header + input.output;
}

function spawnDialog(
  args: string[],
  options: {
    timeoutMs: number;
    env: NodeJS.ProcessEnv;
    secrets: string[];
    targetHost: string;
    ccpsPassword: string;
  },
): Promise<DialogResult> {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn("ssh", args, {
        detached: true,
        stdio: ["pipe", "pipe", "pipe"],
        env: options.env,
      });
    } catch (error) {
      reject(error instanceof Error ? error : new Error("Failed to start ssh."));
      return;
    }

    let settled = false;
    const finish = (result: DialogResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({
        ...result,
        output: redact(result.output, options.secrets),
      });
    };
    const fail = (error: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      stopChild();
      reject(error);
    };
    const stopChild = () => {
      const pid = child.pid;
      if (pid === undefined) return;
      try {
        process.kill(-pid, "SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
    };

    child.on("error", (error) => {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "ENOENT") {
        fail(new Error("The ssh client is not installed on the agent host."));
        return;
      }
      fail(new Error("Failed to start ssh."));
    });

    const io: DialogIo = {
      write(chunk) {
        child.stdin?.write(chunk);
      },
      onData(handler) {
        child.stdout?.on("data", (chunk: Buffer) => handler(chunk.toString("utf8")));
        child.stderr?.on("data", (chunk: Buffer) => handler(chunk.toString("utf8")));
      },
      onClose(handler) {
        child.on("close", () => handler());
      },
    };

    void driveCheckout(io, options)
      .then((result) => {
        child.stdin?.end();
        finish(result);
        setTimeout(stopChild, 1000);
      })
      .catch((error: unknown) => {
        fail(error instanceof Error ? error : new Error("Checkout dialog failed."));
      });

    const timer = setTimeout(() => {
      stopChild();
    }, options.timeoutMs + 5000);
  });
}

function redact(text: string, secrets: string[]): string {
  let redacted = text;
  for (const secret of secrets) {
    if (!secret) continue;
    redacted = redacted.split(secret).join("[redacted]");
  }
  return redacted;
}
