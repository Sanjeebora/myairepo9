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

export const PRETZEL_LOGIN_KEY = "pretzel-login";

export function targetCheckoutCommand(targetHost: string): string {
  return "recovery-script-present";
}
