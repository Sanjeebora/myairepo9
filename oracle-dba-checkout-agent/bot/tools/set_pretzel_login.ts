import { prompt } from "@cursor/bdk";
import { defineTool } from "@cursor/bdk/tools";
import { z } from "zod";
import { PRETZEL_LOGIN_KEY, assertPretzelUser } from "../lib/ssh-checkout.js";

const Result = z.object({
  saved: z.boolean(),
  user: z.string(),
});

type LoginResult = z.infer<typeof Result>;

export default defineTool({
  description: prompt`
    Save the pretzel SSH account and password for later checkouts.
    Use this when the user supplies the pretzel login account and its password.
    Never return or repeat the password.
  `,
  effect: "write",
  inputSchema: z.object({
    user: z.string().describe("Pretzel SSH account name."),
    password: z.string().describe("Password for that pretzel account."),
  }),
  outputSchema: Result,
  dryRunResult: ({ user }): LoginResult => ({
    saved: false,
    user,
  }),
  async execute({ user, password }, ctx): Promise<LoginResult> {
    const account = assertPretzelUser(user);
    if (password.length === 0 || password.length > 256) {
      throw new Error("Pretzel login password must be 1 to 256 characters.");
    }
    await ctx.host.kv.put(PRETZEL_LOGIN_KEY, { user: account, password });
    return { saved: true, user: account };
  },
});
