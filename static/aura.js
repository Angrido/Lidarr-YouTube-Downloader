/* Aurora: a slow, blurred background whose colours come from the album
   cover in focus. UI.setAura(url) extracts a palette from the cover and
   sets --aura-1..3 on :root; components.css draws and animates the layer.
   auraPalette is pure so tests/js/aura.test.js can run it under node. */
(function (root) {
    "use strict";

    var AURA_DEFAULTS = ["#7c5cff", "#4f8bff", "#e04fd8"];
    var BINS = 12;

    function toHsl(r, g, b) {
        r /= 255; g /= 255; b /= 255;
        var max = Math.max(r, g, b), min = Math.min(r, g, b);
        var l = (max + min) / 2, d = max - min, h = 0;
        var s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
        if (d) {
            if (max === r) h = 60 * (((g - b) / d) % 6);
            else if (max === g) h = 60 * ((b - r) / d + 2);
            else h = 60 * ((r - g) / d + 4);
        }
        return [(h + 360) % 360, s, l];
    }

    function toHex(h, s, l) {
        var c = (1 - Math.abs(2 * l - 1)) * s;
        var x = c * (1 - Math.abs(((h / 60) % 2) - 1));
        var m = l - c / 2;
        var rgb = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
            : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
        return "#" + rgb.map(function (v) {
            var n = Math.round((v + m) * 255);
            return (n < 16 ? "0" : "") + n.toString(16);
        }).join("");
    }

    // Dominant vivid colours of an RGBA buffer, most frequent first. Grey,
    // near-black and near-white pixels are ignored; results are pushed into a
    // lightness/saturation band that reads well behind glass. Missing slots
    // keep the default palette's colour at that index.
    function auraPalette(rgba, count) {
        count = count || 3;
        var bins = [];
        for (var b = 0; b < BINS; b++) bins.push({ w: 0, r: 0, g: 0, b: 0 });
        for (var i = 0; i + 3 < rgba.length; i += 4) {
            if (rgba[i + 3] < 128) continue;
            var hsl = toHsl(rgba[i], rgba[i + 1], rgba[i + 2]);
            if (hsl[1] < 0.25 || hsl[2] < 0.12 || hsl[2] > 0.92) continue;
            var bin = bins[Math.floor(hsl[0] / (360 / BINS)) % BINS];
            bin.w += hsl[1];
            bin.r += rgba[i] * hsl[1];
            bin.g += rgba[i + 1] * hsl[1];
            bin.b += rgba[i + 2] * hsl[1];
        }
        var picked = bins.filter(function (x) { return x.w > 0; })
            .sort(function (x, y) { return y.w - x.w; })
            .map(function (x) {
                var c = toHsl(x.r / x.w, x.g / x.w, x.b / x.w);
                return toHex(c[0], Math.max(c[1], 0.55), Math.min(0.62, Math.max(0.45, c[2])));
            });
        var out = [];
        for (var k = 0; k < count; k++) {
            out.push(picked[k] || AURA_DEFAULTS[k % AURA_DEFAULTS.length]);
        }
        return out;
    }

    if (typeof module !== "undefined" && module.exports) {
        module.exports = { auraPalette: auraPalette, AURA_DEFAULTS: AURA_DEFAULTS };
    }
    if (typeof document === "undefined") return;

    var requestId = 0;

    function apply(colours) {
        var style = document.documentElement.style;
        colours.forEach(function (c, i) { style.setProperty("--aura-" + (i + 1), c); });
    }

    function setAura(url) {
        var id = ++requestId;
        if (!url) {
            apply(AURA_DEFAULTS);
            return Promise.resolve();
        }
        return new Promise(function (resolve) {
            var img = new Image();
            img.crossOrigin = "anonymous";
            img.decoding = "async";
            img.onload = function () {
                if (id !== requestId) return resolve();
                var colours = AURA_DEFAULTS;
                try {
                    var canvas = document.createElement("canvas");
                    canvas.width = canvas.height = 24;
                    var ctx = canvas.getContext("2d", { willReadFrequently: true });
                    ctx.drawImage(img, 0, 0, 24, 24);
                    colours = auraPalette(ctx.getImageData(0, 0, 24, 24).data);
                } catch (e) {
                    // A cover from a host without CORS taints the canvas.
                }
                apply(colours);
                resolve();
            };
            img.onerror = function () {
                if (id === requestId) apply(AURA_DEFAULTS);
                resolve();
            };
            img.src = url;
        });
    }

    function mount() {
        if (document.querySelector(".app-aura")) return;
        var layer = document.createElement("div");
        layer.className = "app-aura";
        layer.setAttribute("aria-hidden", "true");
        layer.innerHTML = "<span></span><span></span><span></span>";
        document.body.insertBefore(layer, document.body.firstChild);
        document.addEventListener("visibilitychange", function () {
            layer.classList.toggle("is-paused", document.hidden);
        });
    }

    root.UI = root.UI || {};
    root.UI.auraPalette = auraPalette;
    root.UI.setAura = setAura;
    root.UI.AURA_DEFAULTS = AURA_DEFAULTS;

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
    else mount();
})(typeof window !== "undefined" ? window : this);
