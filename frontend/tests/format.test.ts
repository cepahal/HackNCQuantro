import assert from "node:assert/strict";
import test from "node:test";
import { compact, date, percent, time, unique } from "../lib/format.ts";
import { displayCount, secondsLabel, timeRange } from "../lib/display.ts";

test("time formatting rounds cleanly across minute boundaries and handles negatives", () => {
  assert.equal(time(59.96), "1:00");
  assert.equal(time(59.96, true), "1:00.0");
  assert.equal(time(-5), "-0:05");
  assert.equal(time(-5, true), "-0:05.0");
  assert.equal(time(Number.NaN), "Not assessed");
});

test("percent and count helpers keep invalid and edge values safe", () => {
  assert.equal(percent(1.2), "99%");
  assert.equal(percent(1), "100%");
  assert.equal(percent(0.996), "99%");
  assert.equal(percent(-0.1), "0%");
  assert.equal(percent(Number.NaN), "Not assessed");
  assert.equal(percent(Number.POSITIVE_INFINITY), "Not assessed");
  assert.equal(displayCount(Number.NaN, "Not assessed"), "Not assessed");
  assert.equal(displayCount(Number.POSITIVE_INFINITY, "Not assessed"), "Not assessed");
  assert.equal(compact(Number.NaN), "Unavailable");
  assert.equal(secondsLabel(Number.NaN), "Not assessed");
});

test("the remaining format helpers handle unknown input consistently", () => {
  assert.equal(timeRange(1, 2), "1.0s–2.0s");
  assert.deepEqual(unique(["a", "", "a", "b"]), ["a", "b"]);
  assert.equal(date("not a date"), "Unavailable");
});
