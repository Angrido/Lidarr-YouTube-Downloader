// Run with: node tests/js/motion.test.js (tests/test_frontend_js.py runs it).
const assert = require("assert");
const { tiltAngles } = require("../../static/motion.js");

const near = (a, b) => Math.abs(a - b) < 1e-9;

const tests = {
  "centre is flat"() {
    const t = tiltAngles(50, 50, 100, 100);
    assert.ok(near(t.rx, 0) && near(t.ry, 0), JSON.stringify(t));
  },
  "corner reaches the maximum on both axes"() {
    const t = tiltAngles(0, 0, 100, 100);
    assert.ok(near(Math.abs(t.rx), 4) && near(Math.abs(t.ry), 4), JSON.stringify(t));
  },
  "far outside is clamped"() {
    const t = tiltAngles(1000, -1000, 100, 100);
    assert.ok(Math.abs(t.rx) <= 4 && Math.abs(t.ry) <= 4, JSON.stringify(t));
  },
  "a smaller max is respected"() {
    const t = tiltAngles(0, 0, 100, 100, 2);
    assert.ok(near(Math.abs(t.rx), 2) && near(Math.abs(t.ry), 2), JSON.stringify(t));
  },
  "the card leans toward the pointer"() {
    assert.ok(tiltAngles(90, 50, 100, 100).ry > 0, "right of centre -> ry > 0");
    assert.ok(tiltAngles(50, 10, 100, 100).rx > 0, "above centre -> rx > 0");
  },
  "zero size is flat, not NaN"() {
    const t = tiltAngles(0, 0, 0, 0);
    assert.ok(near(t.rx, 0) && near(t.ry, 0), JSON.stringify(t));
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
