import assert from "node:assert/strict";
import test from "node:test";
import { readConfig } from "../src/config.ts";

const env = { PHOTON_BRIDGE_TOKEN: "test-only-token-with-at-least-32-characters" };

test("terminal mode does not need cloud credentials", () => {
  const result = readConfig(env, []);
  assert.equal(result.cloud, false);
  assert.equal(result.port, 8766);
  assert.equal(result.backendPort, 8000);
  assert.match(result.directory, /[\\/]\.data[\\/]photon$/);
});

test("rejects missing or weak private bridge tokens", () => {
  assert.throws(() => readConfig({}, []));
  assert.throws(() => readConfig({ PHOTON_BRIDGE_TOKEN: "short" }, []));
});

test("managed mode requires both canonical credentials", () => {
  assert.throws(() => readConfig(env, ["--imessage"]));
  assert.throws(() => readConfig({ ...env, SPECTRUM_PROJECT_ID: "test-id" }, ["--imessage"]));
  assert.equal(readConfig({ ...env, SPECTRUM_PROJECT_ID: "test-id", SPECTRUM_PROJECT_SECRET: "test-secret" }, ["--imessage"]).cloud, true);
});

test("rejects unsupported args and invalid bridge ports", () => {
  assert.throws(() => readConfig(env, ["--other"]));
  for (const port of ["0", "80", "65536", "8766junk", "12.5"]) {
    assert.throws(() => readConfig({ ...env, PHOTON_BRIDGE_PORT: port }, []));
    assert.throws(() => readConfig({ ...env, PHOTON_BACKEND_PORT: port }, []));
  }
});
