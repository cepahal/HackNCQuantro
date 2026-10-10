import { join } from "node:path";
import { Spectrum, type Space } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";
import { terminal } from "spectrum-ts/providers/terminal";
import { readConfig } from "./config.ts";
import { createReceiver, hasConversation } from "./receive.ts";
import { createBridgeServer } from "./server.ts";
import { BridgeError, createSummarySender } from "./summary.ts";

async function main(): Promise<void> {
  const config = readConfig(process.env, process.argv.slice(2));
  const cloudApp = config.cloud
    ? await Spectrum({
      projectId: config.projectId, projectSecret: config.projectSecret,
      providers: [imessage.config()], options: { logLevel: "error" }, telemetry: false,
    })
    : undefined;
  const app = cloudApp ?? await Spectrum({ providers: [terminal.config()], options: { logLevel: "error" }, telemetry: false });

  const directory = join(config.directory, "inbox");
  const conversations = new Map<string, Space>();
  const receive = createReceiver({ directory });
  const sendSummary = createSummarySender({
    deliveryDirectory: join(config.directory, "deliveries"),
    resolveConversation: async (id) => {
      const live = conversations.get(id);
      if (live) return live;
      if (cloudApp && await hasConversation(directory, id)) {
        return imessage(cloudApp).space.get(id);
      }
      throw new BridgeError(409, "CONVERSATION_UNAVAILABLE", "Receive a submission in this conversation before sending its report.", false);
    },
  });
  const server = createBridgeServer({ token: config.token, sendSummary });
  let stopping = false;
  const stop = (): void => {
    if (stopping) return;
    stopping = true;
    server.close();
    void app.stop();
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  try {
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(config.port, "127.0.0.1", () => {
        server.removeListener("error", reject);
        resolve();
      });
    });
    server.on("error", () => {
      console.error("The private Photon bridge stopped accepting requests.");
      stop();
    });
    console.log(`Quantro Photon (${config.cloud ? "iMessage" : "terminal"}) ready at http://127.0.0.1:${config.port}.`);
    for await (const [space, message] of app.messages) {
      if (message.direction !== "inbound") continue;
      try {
        await receive(space, message);
        if (await hasConversation(directory, space.id)) {
          conversations.set(space.id, space);
          if (conversations.size > 1000) {
            const oldest = conversations.keys().next().value;
            if (oldest !== undefined) conversations.delete(oldest);
          }
        }
      } catch {
        console.error("A Photon submission could not be handled. Private details were omitted.");
      }
    }
  } finally {
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
    server.close();
    await app.stop();
  }
}

main().catch(() => {
  console.error("Quantro Photon could not start. Check the private env, Node 24, SDK setup and local port. Credentials were omitted.");
  process.exitCode = 1;
});
