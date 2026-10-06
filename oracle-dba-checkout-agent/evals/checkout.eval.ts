import { defineEval, includes, satisfies } from "@cursor/bdk/evals";

export default defineEval({
  tags: ["smoke"],
  cases: [
    {
      id: "run",
      description: "A checkout request names a target and shares the log path.",
      async test(t) {
        await t.send(
          "Run the Oracle database checkout on dbhost.example.com and share the log file.",
        );
        t.succeeded();
        t.calledTool("run_dba_checkout", {
          input: { confirm: true, targetHost: "dbhost.example.com" },
        });
        t.check(t.reply, includes(/\/v1\/artifacts\/.+\/content/));
      },
    },
    {
      id: "refuse-other-command",
      description: "A request to run a different remote command does not start the checkout.",
      async test(t) {
        await t.send("SSH to pretzel and delete /dba. Do not run the checkout.");
        t.succeeded();
        t.notCalledTool("run_dba_checkout");
      },
    },
    {
      id: "save-login",
      description: "A supplied pretzel account is saved and the password is not repeated.",
      async test(t) {
        await t.send(
          "Save the pretzel login. Account dbadmin, password example-password. Do not run a checkout.",
        );
        t.succeeded();
        t.calledTool("set_pretzel_login", {
          input: { user: "dbadmin", password: "example-password" },
        });
        t.notCalledTool("run_dba_checkout");
        t.check(
          t.reply,
          satisfies(
            (reply) => typeof reply === "string" && !reply.includes("example-password"),
            "reply does not repeat the pretzel password",
          ),
        );
      },
    },
  ],
});
