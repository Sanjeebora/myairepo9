import { defineArtifacts } from "@cursor/bdk/artifacts";
import { z } from "zod";

export default defineArtifacts({
  kinds: {
    "dba-checkout-log": {
      description: "Combined output from /dba/bin/dba_checkout on the target server.",
      schema: z.object({
        jumpHost: z.string(),
        targetHost: z.string(),
        script: z.literal("/dba/bin/dba_checkout"),
        exitCode: z.number().int().nullable(),
        timedOut: z.boolean(),
        truncated: z.boolean(),
        startedAt: z.string(),
        finishedAt: z.string(),
        evalFixture: z.boolean(),
      }),
    },
  },
});
