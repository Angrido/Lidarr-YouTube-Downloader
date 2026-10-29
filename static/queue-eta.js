/* Queue ETA: turns the answer of /api/queue/eta into the words the Downloads
   and Library pages show. Pure functions (no DOM), tested by
   tests/js/queue-eta.test.js. */
(function (root) {
    "use strict";

    var MIN = 60, HOUR = 3600, DAY = 86400;
    var BASIS = { "1h": "1 h", "24h": "24 h", "7d": "7 days" };

    function unitFor(seconds) {
        if (seconds < 90 * MIN) return { name: "min", size: MIN };
        if (seconds < 36 * HOUR) return { name: "h", size: HOUR };
        return { name: "day", size: DAY };
    }

    function amount(seconds, unit) {
        var v = seconds / unit.size;
        return v < 10 ? Math.round(v * 10) / 10 : Math.round(v);
    }

    function label(value, unit) {
        if (unit.name === "day") return value === 1 ? "day" : "days";
        return unit.name;
    }

    /* "21–31 days", "25 days" (ends round to one value), "at least 21 days"
       (no upper end). Both ends use the unit of the central value. */
    function formatRange(eta) {
        var unit = unitFor(eta.seconds);
        var lo = amount(Math.min(eta.low, eta.seconds), unit);
        var mid = amount(eta.seconds, unit);
        var central = mid + " " + label(mid, unit);
        if (eta.high == null) return { range: "at least " + lo + " " + label(lo, unit), central: central };
        var hi = amount(eta.high, unit);
        var tail = " " + label(hi, unit);
        return { range: lo === hi ? hi + tail : lo + "–" + hi + tail, central: central };
    }

    function rate(value) {
        return (Math.round(value * 10) / 10).toFixed(1);
    }

    function describe(data, opts) {
        opts = opts || {};
        var fmt = opts.fmt || function (n) { return Number(n).toLocaleString(); };
        var fmtDate = opts.fmtDate || function (d) {
            return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
        };
        var now = opts.now == null ? Date.now() : opts.now;
        if (!data || !data.queue || data.state === "empty") return null;

        var q = data.queue, r = data.rate || {}, eta = data.eta;
        var state = data.state;
        var out = { state: state, tone: "info", icon: "clock", headline: "", detail: "", short: "",
                    hints: [], explain: [] };

        var left = "";
        var finish = "";
        var central = "";
        if (eta) {
            var f = formatRange(eta);
            left = f.range;
            central = f.central;
            finish = fmtDate(new Date(now + eta.seconds * 1000));
        }
        var held = {
            restarting: ["refresh", "A restart is pending — nothing new will start", "Restart pending"],
            paused: ["pause", "Queue paused", "Paused"],
            vpn_rotating: ["refresh", "VPN restarting for a new IP — the queue continues in a moment", "VPN restarting"],
            vpn_blocked: ["alert", "Queue held until the VPN gets a new IP", "Held for the VPN"],
        }[state];

        if (held) {
            out.icon = held[0];
            out.tone = state === "paused" || state === "restarting" ? "info" : "warning";
            out.headline = left ? held[1] + " — about " + left + " left once it runs" : held[1];
            out.short = held[2];
        } else if (state === "insufficient_data" || !eta) {
            out.headline = "Not enough download history to estimate the time left";
            out.short = "";
        } else {
            out.headline = (eta.high == null ? "At least " + left.replace("at least ", "")
                                             : "About " + left) + " left";
            out.short = "≈ " + left + " left";
        }

        var parts = [fmt(q.albums) + " album" + (q.albums === 1 ? "" : "s"),
                     "~" + fmt(q.tracks) + " tracks"];
        if (r.per_min) parts.push(rate(r.per_min) + " tracks/min (" + (BASIS[r.basis] || r.basis) + ")");
        if (eta && !held) parts.push("most likely " + central + ", around " + finish);
        out.detail = parts.join(" · ");

        var w = r.windows || {};
        if ((data.warnings || []).indexOf("slowdown") >= 0 && w["1h"] && !held) {
            out.tone = "warning";
            out.icon = "alert";
            out.hints.push("The last hour ran at " + rate(w["1h"].per_min) + " tracks/min, well under the "
                + (BASIS[r.basis] || r.basis) + " pace — the estimate may be optimistic.");
        }
        if ((data.warnings || []).indexOf("high_failures") >= 0) {
            out.tone = "warning";
            out.icon = "alert";
            out.hints.push("Most tracks are failing right now (blocked addresses or no match on YouTube).");
        }
        if ((data.warnings || []).indexOf("short_history") >= 0) {
            out.hints.push("Based on the " + (BASIS[r.basis] || r.basis) + " pace only: less than a day of history.");
        }

        out.explain.push("Tracks waiting: the missing tracks of every queued album as Lidarr counts them"
            + (q.tracks_estimated ? " (about " + fmt(q.tracks_estimated) + " are estimated at "
                + q.avg_tracks + " per album for albums not in the library cache)" : "") + ".");
        out.explain.push("Pace: download attempts per minute over the " + (BASIS[r.basis] || "last 24 h")
            + ", by the clock — pauses, VPN restarts and idle time are inside it. Failed attempts count: they take their turn too.");
        if (eta) {
            var h = r.hourly || {};
            out.explain.push("Range: from the slower and the quicker quarter of the last 24 hours"
                + (h.slow != null ? " (" + rate(h.slow) + " and " + rate(h.fast) + " tracks/min)" : "")
                + (w["7d"] && w["7d"].per_min ? " and the 7-day pace (" + rate(w["7d"].per_min) + ")" : "")
                + ". It shows how much the pace moved lately, not a guarantee: the quality of the VPN addresses and tracks YouTube cannot match change it.");
        }
        if (data.expected_failures) {
            out.explain.push("About " + fmt(data.expected_failures) + " tracks ("
                + Math.round(data.failure_rate * 100) + " %) are expected to fail; they come back later with a growing delay and are not in this estimate.");
        }
        return out;
    }

    var api = { describe: describe, formatRange: formatRange };
    if (typeof module !== "undefined" && module.exports) module.exports = api;
    else root.QueueEta = api;
})(typeof window !== "undefined" ? window : this);
