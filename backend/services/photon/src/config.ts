import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repository = fileURLToPath(new URL("../../../../", import.meta.url));

export function readConfig(env: NodeJS.ProcessEnv, args: string[]) {
  if (args.some((arg) => arg !== "--imessage")) {
    throw new Error("Use npm start for terminal mode or npm run start:imessage for managed iMessage.");
  }
  const cloud = args.includes("--imessage");
  const token = env.PHOTON_BRIDGE_TOKEN?.trim() ?? "";
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token)) {
    throw new Error("Set a private PHOTON_BRIDGE_TOKEN of 32-128 letters, numbers, underscores or hyphens.");
  }
  const projectId = env.SPECTRUM_PROJECT_ID?.trim() ?? "";
  const projectSecret = env.SPECTRUM_PROJECT_SECRET?.trim() ?? "";
  if (cloud && (!projectId || !projectSecret)) {
    throw new Error("Managed iMessage requires SPECTRUM_PROJECT_ID and SPECTRUM_PROJECT_SECRET.");
  }
  const portText = env.PHOTON_BRIDGE_PORT?.trim() || "8766";
  const port = Number(portText);
  if (!/^\d+$/.test(portText) || !Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error("PHOTON_BRIDGE_PORT must be a port from 1024 to 65535.");
  }
  const backendPortText = env.PHOTON_BACKEND_PORT?.trim() || "8000";
  const backendPort = Number(backendPortText);
  if (!/^\d+$/.test(backendPortText) || !Number.isInteger(backendPort) || backendPort < 1024 || backendPort > 65535) {
    throw new Error("PHOTON_BACKEND_PORT must be a local backend port from 1024 to 65535.");
  }
  return {
    cloud, token, projectId, projectSecret, port, backendPort,
    directory: resolve(repository, env.PHOTON_DATA_DIR?.trim() || ".data/photon"),
  };
}
