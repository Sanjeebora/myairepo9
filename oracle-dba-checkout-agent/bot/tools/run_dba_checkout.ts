import { prompt } from "@cursor/bdk";
import { defineTool } from "@cursor/bdk/tools";
import { z } from "zod";
import {
  CHECKOUT_SCRIPT,
  JUMP_HOST,
  PRETZEL_LOGIN_KEY,
  assertTargetHost,
  loadSshConfig,
  parsePretzelLogin,
  previewLog,
  runRemoteCheckout,
  type CheckoutRun,
  type SshConfig,
} from "../lib/ssh-checkout.js";

const LOCK_KEY = "dba-checkout-lock";

const Result = z.object({
  ok: z.boolean(),
  exitCode: z.number().int().nullable(),
  timedOut: z.boolean(),
  jumpHost: z.string(),
  targetHost: z.string(),
  script: z.literal(CHECKOUT_SCRIPT),
  startedAt: z.string(),
  finishedAt: z.string(),
  artifactId: z.string(),
  downloadPath: z.string(),
  logBytes: z.number().int(),
  truncated: z.boolean(),
  preview: z.string(),
});

type CheckoutResult = z.infer<typeof Result>;

export default defineTool({
  description: prompt`
    Run the Oracle DBA checkout. SSHes only to pretzel.int.thomsonreuters.com,
    runs sudo su - oracle, answers the ccps password prompt, then runs
    /dba/bin/dba_checkout on targetHost with no arguments. confirm must be true.
    Do not pass the pretzel account or password to this tool. Save those with
    set_pretzel_login. This tool reads the saved login, then the environment.
  `,
  effect: "write",
  inputSchema: z.object({
    confirm: z.literal(true).describe("Must be true. Runs the fixed checkout sequence."),
    targetHost: z
      .string()
      .describe("Database server hostname or IPv4 address. Not pretzel."),
  }),
  outputSchema: Result,
  dryRunResult: ({ targetHost }): CheckoutResult => ({
    ok: false,
    exitCode: null,
    timedOut: false,
    jumpHost: JUMP_HOST,
    targetHost,
    script: CHECKOUT_SCRIPT,
    startedAt: "",
    finishedAt: "",
    artifactId: "",
    downloadPath: "",
    logBytes: 0,
    truncated: false,
    preview: "Dry run: the checkout was not executed.",
  }),
  async execute({ targetHost }, ctx): Promise<CheckoutResult> {
    const target = assertTargetHost(targetHost);
    if (ctx.session.purpose === "eval") {
      return publish(ctx, evalConfig(), evalRun(ctx.now(), target));
    }

    const saved = parsePretzelLogin(await ctx.host.kv.get(PRETZEL_LOGIN_KEY));
    const config = await loadSshConfig(process.env, saved);
    const owner = ctx.toolCallId ?? ctx.session.id;
    await acquireLock(ctx, owner, config.timeoutSeconds);
    try {
      const run = await runRemoteCheckout(config, target);
      return await publish(ctx, config, run);
    } finally {
      await releaseLock(ctx, owner);
    }
  },
});

function evalConfig(): SshConfig {
  return {
    jumpHost: JUMP_HOST,
    port: 22,
    user: "eval",
    password: "",
    ccpsPassword: "",
    knownHosts: "",
    timeoutSeconds: 30,
  };
}

function evalRun(now: Date, targetHost: string): CheckoutRun {
  const startedAt = now.toISOString();
  const log = [
    "# Oracle DBA checkout",
    `# jump: eval@${JUMP_HOST}:22`,
    `# target: ${targetHost}`,
    `# script: ${CHECKOUT_SCRIPT}`,
    `# started: ${startedAt}`,
    `# finished: ${startedAt}`,
    "# exit: 0",
    "# timedOut: false",
    "# truncated: false",
    "",
    "eval fixture: checkout was not executed",
    "status=ok",
  ].join("\n");
  return {
    exitCode: 0,
    timedOut: false,
    startedAt,
    finishedAt: startedAt,
    log,
    truncated: false,
    logBytes: Buffer.byteLength(log),
    targetHost,
  };
}

async function publish(
  ctx: {
    now(): Date;
    artifacts: {
      tag(input: {
        kind: string;
        key: string;
        title: string;
        contentType: string;
        contents: string;
        data: {
          jumpHost: string;
          targetHost: string;
          script: typeof CHECKOUT_SCRIPT;
          exitCode: number | null;
          timedOut: boolean;
          truncated: boolean;
          startedAt: string;
          finishedAt: string;
          evalFixture: boolean;
        };
      }): Promise<{ id: string }>;
    };
    session: { id: string; purpose?: string };
    toolCallId?: string;
  },
  config: SshConfig,
  run: CheckoutRun,
): Promise<CheckoutResult> {
  const stamp = run.startedAt.replace(/[:.]/g, "-");
  const record = await ctx.artifacts.tag({
    kind: "dba-checkout-log",
    key: `dba-checkout:${ctx.session.id}:${ctx.toolCallId ?? stamp}`,
    title: `DBA checkout ${run.targetHost} ${stamp}`,
    contentType: "text/plain; charset=utf-8",
    contents: run.log,
    data: {
      jumpHost: config.jumpHost,
      targetHost: run.targetHost,
      script: CHECKOUT_SCRIPT,
      exitCode: run.exitCode,
      timedOut: run.timedOut,
      truncated: run.truncated,
      startedAt: run.startedAt,
      finishedAt: run.finishedAt,
      evalFixture: ctx.session.purpose === "eval",
    },
  });

  return {
    ok: run.exitCode === 0 && !run.timedOut,
    exitCode: run.exitCode,
    timedOut: run.timedOut,
    jumpHost: config.jumpHost,
    targetHost: run.targetHost,
    script: CHECKOUT_SCRIPT,
    startedAt: run.startedAt,
    finishedAt: run.finishedAt,
    artifactId: record.id,
    downloadPath: `/v1/artifacts/${encodeURIComponent(record.id)}/content`,
    logBytes: run.logBytes,
    truncated: run.truncated,
    preview: previewLog(run.log),
  };
}

type LockContext = {
  now(): Date;
  host: {
    kv: {
      get(key: string): Promise<unknown>;
      put(key: string, value: { owner: string; exp: number }): Promise<void>;
      delete(key: string): Promise<void>;
    };
  };
};

async function acquireLock(
  ctx: LockContext,
  owner: string,
  timeoutSeconds: number,
): Promise<void> {
  const now = ctx.now().getTime();
  const current = await ctx.host.kv.get(LOCK_KEY);
  if (isActiveLock(current, now) && current.owner !== owner) {
    throw new Error(
      "A DBA checkout is already running. Wait for it to finish, then try again.",
    );
  }
  await ctx.host.kv.put(LOCK_KEY, {
    owner,
    exp: now + (timeoutSeconds + 60) * 1000,
  });
}

async function releaseLock(ctx: LockContext, owner: string): Promise<void> {
  const current = await ctx.host.kv.get(LOCK_KEY);
  if (isLock(current) && current.owner === owner) {
    await ctx.host.kv.delete(LOCK_KEY);
  }
}

function isLock(value: unknown): value is { owner: string; exp: number } {
  if (typeof value !== "object" || value === null) return false;
  const record = value as { owner?: unknown; exp?: unknown };
  return typeof record.owner === "string" && typeof record.exp === "number";
}

function isActiveLock(
  value: unknown,
  now: number,
): value is { owner: string; exp: number } {
  return isLock(value) && value.exp > now;
}
