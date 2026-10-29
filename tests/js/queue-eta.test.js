// Run with: node tests/js/queue-eta.test.js (tests/test_frontend_js.py runs it).
const assert = require("assert");
const { describe, formatRange } = require("../../static/queue-eta.js");

const DAY = 86400;
const OPTS = { fmt: n => String(n), fmtDate: () => "23 Nov", now: 0 };

function data(over) {
  return Object.assign({
    state: "ok",
    queue: { albums: 9585, tracks: 139000, tracks_estimated: 0, avg_tracks: 14.5 },
    rate: {
      basis: "24h", per_min: 3.3,
      windows: { "1h": { per_min: 3.9 }, "24h": { per_min: 3.3 }, "7d": { per_min: 4.5 } },
      hourly: { slow: 2.4, fast: 3.9 },
    },
    failure_rate: 0.16, expected_failures: 22000,
    eta: { seconds: 29 * DAY, low: 21 * DAY, high: 40 * DAY },
    warnings: [],
  }, over);
}

const tests = {
  "range in days with the central value"() {
    const f = formatRange({ seconds: 29 * DAY, low: 21 * DAY, high: 40 * DAY });
    assert.strictEqual(f.range, "21–40 days");
    assert.strictEqual(f.central, "29 days");
  },
  "the unit follows the central value"() {
    assert.strictEqual(formatRange({ seconds: 5 * 3600, low: 4 * 3600, high: 7 * 3600 }).range, "4–7 h");
    assert.strictEqual(formatRange({ seconds: 20 * 60, low: 15 * 60, high: 30 * 60 }).range, "15–30 min");
  },
  "small spans keep one decimal"() {
    assert.strictEqual(formatRange({ seconds: 2.5 * DAY, low: 2 * DAY, high: 3.2 * DAY }).range, "2–3.2 days");
  },
  "ends that round to the same value collapse"() {
    assert.strictEqual(formatRange({ seconds: 30 * DAY, low: 29.8 * DAY, high: 30.2 * DAY }).range, "30 days");
  },
  "no upper end reads as at least"() {
    assert.strictEqual(formatRange({ seconds: 30 * DAY, low: 21 * DAY, high: null }).range, "at least 21 days");
  },
  "ok state: headline, detail and short form"() {
    const d = describe(data(), OPTS);
    assert.strictEqual(d.headline, "About 21–40 days left");
    assert.strictEqual(d.short, "≈ 21–40 days left");
    assert.strictEqual(d.tone, "info");
    assert.ok(d.detail.includes("9585 albums"), d.detail);
    assert.ok(d.detail.includes("~139000 tracks"), d.detail);
    assert.ok(d.detail.includes("3.3 tracks/min (24 h)"), d.detail);
    assert.ok(d.detail.includes("most likely 29 days, around 23 Nov"), d.detail);
  },
  "no upper end headline"() {
    const d = describe(data({ eta: { seconds: 30 * DAY, low: 21 * DAY, high: null } }), OPTS);
    assert.strictEqual(d.headline, "At least 21 days left");
  },
  "an empty queue shows nothing"() {
    assert.strictEqual(describe(data({ state: "empty" }), OPTS), null);
    assert.strictEqual(describe(null, OPTS), null);
  },
  "paused keeps the time once it resumes"() {
    const d = describe(data({ state: "paused" }), OPTS);
    assert.strictEqual(d.icon, "pause");
    assert.strictEqual(d.short, "Paused");
    assert.ok(d.headline.startsWith("Queue paused — about 21–40 days left"), d.headline);
    assert.ok(!d.detail.includes("around"), d.detail);
  },
  "VPN states warn"() {
    const rot = describe(data({ state: "vpn_rotating" }), OPTS);
    assert.strictEqual(rot.tone, "warning");
    assert.ok(rot.headline.includes("VPN restarting"));
    const blk = describe(data({ state: "vpn_blocked" }), OPTS);
    assert.ok(blk.headline.includes("held until the VPN gets a new IP"));
  },
  "not enough history"() {
    const d = describe(data({ state: "insufficient_data", eta: null, rate: { basis: null, per_min: null } }), OPTS);
    assert.ok(d.headline.startsWith("Not enough download history"));
    assert.strictEqual(d.short, "");
    assert.ok(d.detail.includes("~139000 tracks"));
  },
  "a slowdown warns with the last hour's pace"() {
    const d = describe(data({
      warnings: ["slowdown"],
      rate: { basis: "24h", per_min: 3.3, windows: { "1h": { per_min: 1.2 }, "24h": { per_min: 3.3 } }, hourly: {} },
    }), OPTS);
    assert.strictEqual(d.tone, "warning");
    assert.ok(d.hints[0].includes("1.2 tracks/min"), d.hints[0]);
  },
  "no slowdown hint while the queue is held"() {
    const d = describe(data({ state: "paused", warnings: ["slowdown"] }), OPTS);
    assert.deepStrictEqual(d.hints, []);
  },
  "failures warn"() {
    const d = describe(data({ warnings: ["high_failures"] }), OPTS);
    assert.strictEqual(d.tone, "warning");
    assert.ok(d.hints[0].includes("Most tracks are failing"));
  },
  "the explanation covers tracks, pace, range and expected failures"() {
    const d = describe(data({ queue: { albums: 3, tracks: 100, tracks_estimated: 30, avg_tracks: 14.5 } }), OPTS);
    const text = d.explain.join("\n");
    assert.ok(text.includes("about 30 are estimated at 14.5"), text);
    assert.ok(text.includes("pauses, VPN restarts and idle time"), text);
    assert.ok(text.includes("2.4 and 3.9 tracks/min"), text);
    assert.ok(text.includes("7-day pace (4.5)"), text);
    assert.ok(text.includes("22000 tracks (16 %)"), text);
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}: ${e.message}`);
  }
}
process.exit(failed ? 1 : 0);
