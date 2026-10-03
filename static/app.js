(function () {
    "use strict";

    var THEME_KEY = "theme";
    var root = document.documentElement;
    var mql = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

    function readTheme() {
        try {
            var v = localStorage.getItem(THEME_KEY);
            return v === "light" || v === "dark" ? v : "auto";
        } catch (e) {
            return "auto";
        }
    }

    function effectiveTheme(pref) {
        if (pref === "light" || pref === "dark") return pref;
        return mql && mql.matches ? "dark" : "light";
    }

    function syncThemeColor() {
        var meta = document.querySelector('meta[name="theme-color"]:not([media])');
        var dark = effectiveTheme(readTheme()) === "dark";
        if (meta) meta.setAttribute("content", dark ? "#0a0a0c" : "#f5f5f7");
    }

    function applyTheme(pref) {
        if (pref === "light" || pref === "dark") root.setAttribute("data-theme", pref);
        else root.removeAttribute("data-theme");
        root.setAttribute("data-theme-pref", pref);
        if (document.body) {
            document.body.classList.toggle("light", effectiveTheme(pref) === "light");
        }
        syncThemeColor();
        document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
            var label = { auto: "Theme: automatic", light: "Theme: light", dark: "Theme: dark" }[pref];
            btn.setAttribute("aria-label", label);
            btn.setAttribute("title", label);
            btn.innerHTML = icon(pref === "auto" ? "monitor" : pref === "light" ? "sun" : "moon");
        });
        try {
            window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: effectiveTheme(pref), preference: pref } }));
        } catch (e) {}
    }

    function setTheme(pref) {
        try {
            if (pref === "auto") localStorage.removeItem(THEME_KEY);
            else localStorage.setItem(THEME_KEY, pref);
        } catch (e) {}
        applyTheme(pref);
    }

    function cycleTheme() {
        var order = ["auto", "light", "dark"];
        var next = order[(order.indexOf(readTheme()) + 1) % order.length];
        setTheme(next);
        toast({ auto: "Theme follows your system", light: "Light theme", dark: "Dark theme" }[next], { type: "info", duration: 1600 });
    }

    if (mql) {
        var onScheme = function () {
            if (readTheme() === "auto") applyTheme("auto");
        };
        if (mql.addEventListener) mql.addEventListener("change", onScheme);
        else if (mql.addListener) mql.addListener(onScheme);
    }

    function escapeHtml(value) {
        if (value === null || value === undefined) return "";
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function icon(name, cls) {
        return '<svg class="ico' + (cls ? " " + cls : "") + '" aria-hidden="true" focusable="false"><use href="/static/icons.svg#i-' + name + '"></use></svg>';
    }

    function hydrateIcons(scope) {
        (scope || document).querySelectorAll("[data-icon]").forEach(function (el) {
            if (el.dataset.iconDone) return;
            el.insertAdjacentHTML("afterbegin", icon(el.getAttribute("data-icon"), el.getAttribute("data-icon-class") || ""));
            el.dataset.iconDone = "1";
        });
    }

    var toastHost = null;

    function ensureToastHost() {
        if (toastHost && document.body.contains(toastHost)) return toastHost;
        toastHost = document.createElement("div");
        toastHost.className = "ui-toasts";
        toastHost.setAttribute("role", "region");
        toastHost.setAttribute("aria-label", "Notifications");
        toastHost.setAttribute("aria-live", "polite");
        document.body.appendChild(toastHost);
        return toastHost;
    }

    var TOAST_ICONS = { success: "check-circle", error: "x-circle", danger: "x-circle", warning: "alert", info: "info" };

    function toast(message, opts) {
        opts = opts || {};
        var type = opts.type || "info";
        var host = ensureToastHost();
        while (host.children.length >= 4) host.removeChild(host.firstChild);
        var el = document.createElement("div");
        el.className = "ui-toast ui-toast-" + type;
        el.setAttribute("role", type === "error" || type === "danger" ? "alert" : "status");
        el.innerHTML = icon(TOAST_ICONS[type] || "info") + '<div class="ui-toast-msg"></div>';
        el.querySelector(".ui-toast-msg").textContent = message;
        var timer = null;
        function dismiss() {
            if (timer) clearTimeout(timer);
            if (!el.parentNode || el.classList.contains("is-leaving")) return;
            el.classList.add("is-leaving");
            setTimeout(function () {
                if (el.parentNode) el.parentNode.removeChild(el);
            }, 240);
        }
        if (opts.action && typeof opts.action.onClick === "function") {
            var act = document.createElement("button");
            act.type = "button";
            act.className = "ui-btn ui-btn-sm ui-btn-tinted ui-toast-action";
            act.textContent = opts.action.label || "Undo";
            act.addEventListener("click", function () {
                opts.action.onClick();
                dismiss();
            });
            el.appendChild(act);
        }
        var close = document.createElement("button");
        close.type = "button";
        close.className = "ui-btn ui-btn-ghost ui-btn-icon ui-btn-sm";
        close.setAttribute("aria-label", "Dismiss notification");
        close.innerHTML = icon("x");
        close.addEventListener("click", dismiss);
        el.appendChild(close);
        host.appendChild(el);
        var duration = opts.duration === undefined ? (type === "error" ? 6000 : 3800) : opts.duration;
        function arm() {
            if (duration > 0) timer = setTimeout(dismiss, duration);
        }
        el.addEventListener("mouseenter", function () {
            if (timer) clearTimeout(timer);
        });
        el.addEventListener("mouseleave", arm);
        arm();
        return { dismiss: dismiss, el: el };
    }

    var openModals = [];
    var FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

    function resolveModal(target) {
        return typeof target === "string" ? document.getElementById(target) : target;
    }

    function openModal(target) {
        var modal = resolveModal(target);
        if (!modal) return;
        if (openModals.indexOf(modal) !== -1) return;
        modal.__returnFocus = document.activeElement;
        modal.classList.remove("is-closing");
        modal.hidden = false;
        modal.style.display = "";
        modal.classList.add("active", "show", "open");
        modal.setAttribute("aria-hidden", "false");
        openModals.push(modal);
        document.documentElement.style.overflow = "hidden";
        var box = modal.querySelector(".ui-modal-box") || modal;
        if (!box.hasAttribute("role")) box.setAttribute("role", "dialog");
        box.setAttribute("aria-modal", "true");
        requestAnimationFrame(function () {
            var focusEl = modal.querySelector("[autofocus]") || modal.querySelector(FOCUSABLE);
            if (focusEl) focusEl.focus({ preventScroll: true });
        });
    }

    function closeModal(target, immediate) {
        var modal = resolveModal(target);
        if (!modal || modal.hidden) return;
        var idx = openModals.indexOf(modal);
        if (idx !== -1) openModals.splice(idx, 1);
        function finish() {
            modal.hidden = true;
            modal.classList.remove("is-closing", "active", "show", "open");
            modal.setAttribute("aria-hidden", "true");
            if (!openModals.length) document.documentElement.style.overflow = "";
            var rf = modal.__returnFocus;
            if (rf && typeof rf.focus === "function" && document.contains(rf)) rf.focus({ preventScroll: true });
            modal.dispatchEvent(new CustomEvent("modal:closed"));
        }
        if (immediate || prefersReducedMotion()) {
            finish();
            return;
        }
        modal.classList.add("is-closing");
        setTimeout(finish, 200);
    }

    function prefersReducedMotion() {
        return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    }

    document.addEventListener("keydown", function (e) {
        if (!openModals.length) return;
        var top = openModals[openModals.length - 1];
        if (e.key === "Escape") {
            if (top.getAttribute("data-dismissible") === "false") return;
            e.preventDefault();
            closeModal(top);
            return;
        }
        if (e.key === "Tab") {
            var items = Array.prototype.filter.call(top.querySelectorAll(FOCUSABLE), function (n) {
                return n.offsetParent !== null || n === document.activeElement;
            });
            if (!items.length) return;
            var first = items[0];
            var last = items[items.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        }
    });

    document.addEventListener("keydown", function (e) {
        if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || openModals.length) return;
        var t = e.target;
        if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
        var field = document.querySelector('.app-main input[type="search"]');
        if (!field || field.offsetParent === null) return;
        e.preventDefault();
        field.focus();
        field.select();
    });

    document.addEventListener("mousedown", function (e) {
        var modal = e.target;
        if (modal && modal.classList && modal.classList.contains("ui-modal") && openModals.indexOf(modal) !== -1) {
            if (modal.getAttribute("data-dismissible") === "false") return;
            modal.__backdropDown = true;
        }
    });

    document.addEventListener("click", function (e) {
        var t = e.target;
        if (t && t.classList && t.classList.contains("ui-modal") && t.__backdropDown) {
            t.__backdropDown = false;
            closeModal(t);
            return;
        }
        var closer = t && t.closest ? t.closest("[data-modal-close]") : null;
        if (closer) {
            var m = closer.closest(".ui-modal");
            if (m) closeModal(m);
            return;
        }
        var opener = t && t.closest ? t.closest("[data-modal-open]") : null;
        if (opener) {
            openModal(opener.getAttribute("data-modal-open"));
            return;
        }
        var themeBtn = t && t.closest ? t.closest("[data-theme-toggle]") : null;
        if (themeBtn) cycleTheme();
    });

    function confirmDialog(opts) {
        opts = typeof opts === "string" ? { message: opts } : opts || {};
        return new Promise(function (resolve) {
            var wrap = document.createElement("div");
            wrap.className = "ui-modal ui-modal-sm";
            wrap.hidden = true;
            wrap.innerHTML =
                '<div class="ui-modal-box" role="alertdialog" aria-labelledby="ui-confirm-title" aria-describedby="ui-confirm-msg">' +
                '<div class="ui-modal-grabber"></div>' +
                '<div class="ui-modal-head"><h2 class="ui-modal-title" id="ui-confirm-title"></h2></div>' +
                '<div class="ui-modal-body"><p class="t-2" id="ui-confirm-msg"></p></div>' +
                '<div class="ui-modal-foot"><button type="button" class="ui-btn ui-btn-secondary" data-act="cancel"></button>' +
                '<button type="button" class="ui-btn" data-act="ok"></button></div></div>';
            wrap.querySelector(".ui-modal-title").textContent = opts.title || "Are you sure?";
            wrap.querySelector("#ui-confirm-msg").textContent = opts.message || "";
            var ok = wrap.querySelector('[data-act="ok"]');
            ok.textContent = opts.confirmLabel || "Confirm";
            ok.classList.add(opts.danger ? "ui-btn-danger-solid" : "ui-btn-primary");
            (opts.danger ? wrap.querySelector('[data-act="cancel"]') : ok).setAttribute("autofocus", "");
            wrap.querySelector('[data-act="cancel"]').textContent = opts.cancelLabel || "Cancel";
            document.body.appendChild(wrap);
            var result = false;
            wrap.addEventListener("click", function (e) {
                var b = e.target.closest("[data-act]");
                if (!b) return;
                result = b.getAttribute("data-act") === "ok";
                closeModal(wrap);
            });
            wrap.addEventListener("modal:closed", function () {
                if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
                resolve(result);
            });
            openModal(wrap);
        });
    }

    function setBusy(btn, busy, label) {
        if (!btn) return;
        if (busy) {
            if (btn.getAttribute("aria-busy") === "true") return;
            btn.__label = btn.innerHTML;
            btn.setAttribute("aria-busy", "true");
            btn.disabled = true;
            btn.innerHTML = '<span class="ui-spinner ui-spinner-sm" aria-hidden="true"></span>' + (label ? "<span>" + escapeHtml(label) + "</span>" : "");
        } else {
            btn.removeAttribute("aria-busy");
            btn.disabled = false;
            if (btn.__label !== undefined) btn.innerHTML = btn.__label;
        }
    }

    function guard(btn, fn, label) {
        if (!btn || btn.getAttribute("aria-busy") === "true") return Promise.resolve();
        setBusy(btn, true, label);
        var p;
        try {
            p = Promise.resolve(fn());
        } catch (e) {
            p = Promise.reject(e);
        }
        return p.then(function (v) {
            setBusy(btn, false);
            return v;
        }, function (err) {
            setBusy(btn, false);
            throw err;
        });
    }

    function fetchJSON(url, opts) {
        opts = opts || {};
        var init = { method: opts.method || "GET", headers: Object.assign({}, opts.headers || {}) };
        if (opts.body !== undefined) {
            if (opts.body instanceof FormData) {
                init.body = opts.body;
            } else {
                init.body = JSON.stringify(opts.body);
                init.headers["Content-Type"] = "application/json";
            }
        }
        if (opts.signal) init.signal = opts.signal;
        return fetch(url, init).then(function (res) {
            return res.text().then(function (txt) {
                var data = null;
                try {
                    data = txt ? JSON.parse(txt) : null;
                } catch (e) {
                    data = null;
                }
                if (!res.ok) {
                    var msg = (data && (data.message || data.error)) || ("Request failed (HTTP " + res.status + ")");
                    var err = new Error(msg);
                    err.status = res.status;
                    err.data = data;
                    throw err;
                }
                return data;
            });
        });
    }

    function poll(fn, interval, opts) {
        opts = opts || {};
        var timer = null;
        var stopped = false;
        var running = false;
        function schedule(ms) {
            if (stopped) return;
            clearTimeout(timer);
            timer = setTimeout(tick, ms);
        }
        function tick() {
            if (stopped) return;
            if (document.hidden && !opts.background) {
                return;
            }
            if (running) {
                schedule(interval);
                return;
            }
            running = true;
            Promise.resolve()
                .then(fn)
                .catch(function () {})
                .then(function () {
                    running = false;
                    schedule(typeof interval === "function" ? interval() : interval);
                });
        }
        function onVis() {
            if (!document.hidden) schedule(0);
        }
        document.addEventListener("visibilitychange", onVis);
        if (opts.immediate === false) schedule(typeof interval === "function" ? interval() : interval);
        else tick();
        return {
            stop: function () {
                stopped = true;
                clearTimeout(timer);
                document.removeEventListener("visibilitychange", onVis);
            },
            now: function () {
                schedule(0);
            }
        };
    }

    function relativeTime(ts) {
        if (!ts) return "";
        var d = typeof ts === "number" ? new Date(ts < 1e12 ? ts * 1000 : ts) : new Date(ts);
        var s = Math.round((Date.now() - d.getTime()) / 1000);
        if (isNaN(s)) return "";
        if (s < 45) return "just now";
        var m = Math.round(s / 60);
        if (m < 60) return m + " min ago";
        var h = Math.round(m / 60);
        if (h < 24) return h + " h ago";
        var days = Math.round(h / 24);
        if (days < 7) return days + (days === 1 ? " day ago" : " days ago");
        return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    }

    function formatDuration(sec) {
        sec = Math.max(0, Math.round(Number(sec) || 0));
        var h = Math.floor(sec / 3600);
        var m = Math.floor((sec % 3600) / 60);
        var s = sec % 60;
        var mm = h ? String(m).padStart(2, "0") : String(m);
        return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
    }

    var navTimer = null;

    function updateNavBadges() {
        var badges = document.querySelectorAll("[data-nav-queue]");
        var lives = document.querySelectorAll("[data-nav-live]");
        if (!badges.length && !lives.length) return Promise.resolve();
        return fetchJSON("/api/stats").then(function (q) {
            if (!q) return;
            if (typeof q.queued === "number") {
                var n = q.queued;
                badges.forEach(function (b) {
                    b.textContent = n > 99 ? "99+" : String(n);
                    b.setAttribute("data-count", String(n));
                    b.setAttribute("aria-label", n + " in queue");
                });
            }
            lives.forEach(function (l) {
                l.hidden = !q.active;
            });
        });
    }

    function initNav() {
        var nav = document.querySelector(".app-nav");
        if (nav) {
            var indicator = nav.querySelector(".app-nav-indicator");
            var current = nav.querySelector('[aria-current="page"]');
            if (indicator && current) {
                nav.classList.add("has-indicator");
                var place = function (el, animate) {
                    if (!animate) indicator.style.transition = "none";
                    indicator.style.transform = "translateY(" + el.offsetTop + "px)";
                    indicator.style.height = el.offsetHeight + "px";
                    indicator.style.opacity = "1";
                    if (!animate) {
                        indicator.getBoundingClientRect();
                        indicator.style.transition = "";
                    }
                };
                var prev = null;
                try {
                    prev = sessionStorage.getItem("navIndicatorFrom");
                } catch (e) {}
                var prevEl = prev ? nav.querySelector('[data-nav="' + prev + '"]') : null;
                if (prevEl && prevEl !== current && !prefersReducedMotion()) {
                    place(prevEl, false);
                    requestAnimationFrame(function () {
                        place(current, true);
                    });
                } else {
                    place(current, false);
                }
                window.addEventListener("resize", function () {
                    place(current, false);
                });
                nav.addEventListener("click", function (e) {
                    var a = e.target.closest("[data-nav]");
                    if (!a) return;
                    try {
                        sessionStorage.setItem("navIndicatorFrom", current.getAttribute("data-nav"));
                    } catch (err) {}
                });
            }
        }
        if (document.querySelector("[data-nav-queue],[data-nav-live]")) {
            navTimer = poll(updateNavBadges, 5000);
        }
    }

    function registerServiceWorker() {
        if (!("serviceWorker" in navigator)) return;
        window.addEventListener("load", function () {
            navigator.serviceWorker.register("/sw.js").catch(function () {});
        });
    }

    function initDropzones(scope) {
        (scope || document).querySelectorAll(".ui-dropzone").forEach(function (zone) {
            if (zone.__dz) return;
            zone.__dz = true;
            ["dragenter", "dragover"].forEach(function (ev) {
                zone.addEventListener(ev, function (e) {
                    e.preventDefault();
                    zone.classList.add("is-dragover");
                });
            });
            ["dragleave", "dragend", "drop"].forEach(function (ev) {
                zone.addEventListener(ev, function () {
                    zone.classList.remove("is-dragover");
                });
            });
        });
    }

    window.UI = {
        icon: icon,
        escape: escapeHtml,
        toast: toast,
        openModal: openModal,
        closeModal: closeModal,
        confirm: confirmDialog,
        setBusy: setBusy,
        guard: guard,
        fetchJSON: fetchJSON,
        poll: poll,
        relativeTime: relativeTime,
        formatDuration: formatDuration,
        setTheme: setTheme,
        getTheme: readTheme,
        effectiveTheme: function () {
            return effectiveTheme(readTheme());
        },
        hydrateIcons: hydrateIcons,
        refreshNav: function () {
            if (navTimer) navTimer.now();
        },
        prefersReducedMotion: prefersReducedMotion
    };

    applyTheme(readTheme());
    registerServiceWorker();

    function ready() {
        applyTheme(readTheme());
        hydrateIcons();
        initNav();
        initDropzones();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
    else ready();
})();
