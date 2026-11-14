// Pure helpers of perf.js (task 8.8, Requirement 2.10). The probe itself needs a browser.
import test from "node:test";
import assert from "node:assert/strict";
import { createRollingAverage, createSlowFrameMonitor, estimateTextureBytes, perfEnabled, startPerfProbe } from "../../skeleton/office/static/perf.js";

test("perfEnabled is true only for perf=1", () => {
  assert.equal(perfEnabled("?perf=1"), true);
  assert.equal(perfEnabled("?mock=1&perf=1"), true);
  for (const s of ["", "?perf=0", "?perf=true", "?mock=1", undefined]) assert.equal(perfEnabled(s), false);
});

test("startPerfProbe does nothing without the flag", () => {
  const office = new Proxy({}, { get() { throw new Error("touched the office"); } });
  assert.equal(startPerfProbe(office, { search: "?mock=1" }), null);
});

test("rolling average keeps the last N values", () => {
  const r = createRollingAverage(3);
  for (const v of [10, 20, 30, 40]) r.push(v);
  assert.equal(r.count, 3);
  assert.equal(r.average, 30);
  assert.equal(r.max, 40);
  r.reset();
  assert.equal(r.average, 0);
});

test("texture estimate: 1024x1024 RGBA with mipmaps is about 5.33 MB", () => {
  assert.equal(estimateTextureBytes(1024, 1024, false), 4 * 1024 * 1024);
  assert.ok(Math.abs(estimateTextureBytes(1024, 1024, true) / 1048576 - 5.333) < 0.01);
  assert.equal(estimateTextureBytes(0, 10), 0);
});

function feed(monitor, ms, seconds) {
  let fired = 0;
  for (let t = 0; t < seconds; t += ms / 1000) if (monitor.push(ms)) fired++;
  return fired;
}

test("slow-frame monitor fires once after 3 s of frames above 40 ms (plus warm-up)", () => {
  const m = createSlowFrameMonitor();
  assert.equal(feed(m, 60, 1.9), 0);   // warm-up
  assert.equal(feed(m, 60, 2.5), 0);   // slow for less than 3 s
  assert.equal(feed(m, 60, 1.5), 1);   // now past 3 s
  assert.equal(feed(m, 60, 10), 0);    // only once
});

test("slow-frame monitor stays quiet at 30 fps and when the slowness is short", () => {
  assert.equal(feed(createSlowFrameMonitor(), 33, 30), 0);
  const m = createSlowFrameMonitor();
  feed(m, 16, 3);
  assert.equal(feed(m, 80, 2), 0);
  assert.equal(feed(m, 16, 10), 0);
});
