/* Motion helpers for the design system: cover tilt, staggered entry and
   counting numbers. Visual rules live in components.css; everything here
   stands down under prefers-reduced-motion. tiltAngles is pure so
   tests/js/motion.test.js can run it under node. */
(function (root) {
    "use strict";

    // Degrees a card tilts toward a pointer at (px, py) inside a w x h box:
    // 0 at the centre, +/-max at the edges, clamped outside.
    function tiltAngles(px, py, width, height, max) {
        max = max === undefined ? 4 : max;
        if (!(width > 0) || !(height > 0)) return { rx: 0, ry: 0 };
        var clamp = function (v) { return Math.max(-max, Math.min(max, v)); };
        return {
            rx: clamp((0.5 - py / height) * 2 * max),
            ry: clamp((px / width - 0.5) * 2 * max),
        };
    }

    if (typeof module !== "undefined" && module.exports) {
        module.exports = { tiltAngles: tiltAngles };
    }
    if (typeof document === "undefined") return;

    var reduced = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    var finePointer = window.matchMedia ? window.matchMedia("(hover: hover) and (pointer: fine)") : null;
    var isReduced = function () { return !!(reduced && reduced.matches); };

    // Cards tilt toward the pointer and a sheen follows it. One delegated
    // listener for any number of cards, at most one update per frame.
    function initTilt() {
        var active = null, frame = 0, last = null;
        function reset(card) {
            ["--rx", "--ry"].forEach(function (p) { card.style.removeProperty(p); });
            card.classList.remove("is-tilting");
        }
        document.addEventListener("pointermove", function (e) {
            if (isReduced() || !(finePointer && finePointer.matches)) return;
            last = e;
            if (frame) return;
            frame = requestAnimationFrame(function () {
                frame = 0;
                var card = last.target.closest && last.target.closest(".ui-album");
                if (active && active !== card) reset(active);
                active = card;
                if (!card) return;
                var box = card.getBoundingClientRect();
                var x = last.clientX - box.left, y = last.clientY - box.top;
                var t = tiltAngles(x, y, box.width, box.height);
                card.style.setProperty("--rx", t.rx.toFixed(2) + "deg");
                card.style.setProperty("--ry", t.ry.toFixed(2) + "deg");
                card.style.setProperty("--mx", (100 * x / box.width).toFixed(1) + "%");
                card.style.setProperty("--my", (100 * y / box.height).toFixed(1) + "%");
                card.classList.add("is-tilting");
            });
        }, { passive: true });
        document.addEventListener("pointerleave", function () {
            if (active) reset(active);
            active = null;
        });
    }

    // First render of a list: children rise in one after another (first 12).
    // The class is removed afterwards so later re-renders don't replay it.
    function stagger(container) {
        if (!container || isReduced()) return;
        Array.prototype.slice.call(container.children, 0, 12).forEach(function (el, i) {
            el.style.setProperty("--i", i);
        });
        container.classList.add("is-entering");
        setTimeout(function () { container.classList.remove("is-entering"); }, 900);
    }

    // Count from the number shown to `value`, written through `format`
    // (default String). Non-numbers and reduced motion just set the text.
    function countTo(el, value, ms, format) {
        if (!el) return;
        format = format || String;
        var to = Number(value);
        var from = parseFloat(String(el.textContent).replace(/[^\d.-]/g, ""));
        if (el._countFrame) cancelAnimationFrame(el._countFrame);
        if (isReduced() || !isFinite(to) || !isFinite(from) || from === to) {
            el.textContent = format(value);
            return;
        }
        var start = null, dur = ms || 600;
        var step = function (now) {
            if (start === null) start = now;
            var p = Math.min(1, (now - start) / dur);
            var eased = 1 - Math.pow(1 - p, 3);
            el.textContent = format(Math.round(from + (to - from) * eased));
            el._countFrame = p < 1 ? requestAnimationFrame(step) : 0;
            if (p === 1) el.textContent = format(value);
        };
        el._countFrame = requestAnimationFrame(step);
    }

    root.UI = root.UI || {};
    root.UI.tiltAngles = tiltAngles;
    root.UI.stagger = stagger;
    root.UI.countTo = countTo;

    function ready() {
        initTilt();
        document.querySelectorAll("[data-stagger]").forEach(stagger);
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
    else ready();
})(typeof window !== "undefined" ? window : this);
