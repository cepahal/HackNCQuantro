import assert from "node:assert/strict";
import { basename } from "node:path";
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

test("public sharing is optional and requires its file to remain inside the data directory", () => {
  assert.equal(readConfig(env, []).publicReportsFile, undefined);
  assert.throws(() => readConfig({ ...env, PHOTON_PUBLIC_REPORTS_FILE: ".data/photon/public-reports.json" }, []));
  for (const file of [".data/outside.json", ".data/photon", ".data/photon/../../outside.json"]) {
    assert.throws(() => readConfig({ ...env, REPORT_BASE_URL: "https://quantro.example", PHOTON_PUBLIC_REPORTS_FILE: file }, []));
  }
  const config = readConfig({ ...env, REPORT_BASE_URL: "https://quantro.example", PHOTON_PUBLIC_REPORTS_FILE: ".data/photon/public-reports.json" }, []);
  assert.equal(basename(config.publicReportsFile!), "public-reports.json");
  assert.equal(config.reportBaseUrl, "https://quantro.example");
});

test("restart reconciliation is read-only unless the operator explicitly resumes saved iMessage requests", () => {
  const cloudEnv = { ...env, SPECTRUM_PROJECT_ID: "test-id", SPECTRUM_PROJECT_SECRET: "test-secret" };
  assert.equal(readConfig(env, []).resumeSaved, false);
  assert.equal(readConfig(cloudEnv, ["--imessage"]).resumeSaved, false);
  assert.equal(readConfig(cloudEnv, ["--imessage", "--resume-saved"]).resumeSaved, true);
  assert.throws(() => readConfig(env, ["--resume-saved"]));
});
