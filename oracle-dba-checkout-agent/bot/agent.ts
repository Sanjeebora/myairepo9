import { defineAgent } from "@cursor/bdk";

export default defineAgent({
  name: "Oracle DBA checkout",
  description:
    "From pretzel, becomes oracle, runs ccps, then runs /dba/bin/dba_checkout on the requested target and returns a downloadable log.",
  model: {
    id: "grok-4.5",
    params: [
      { id: "effort", value: "high" },
      { id: "fast", value: "true" },
    ],
  },
  tools: [],
  local: {
    sandbox: true,
  },
  concurrency: { maxRunningTurns: 2 },
  hosting: {
    egressDomains: ["pretzel.int.thomsonreuters.com:22"],
    secretNames: [
      "ORACLE_DBA_SSH_USER",
      "ORACLE_DBA_SSH_PASSWORD",
      "ORACLE_DBA_SSH_KNOWN_HOSTS",
      "ORACLE_DBA_CCPS_PASSWORD",
    ],
  },
});
