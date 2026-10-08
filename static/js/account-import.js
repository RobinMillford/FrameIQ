/* Account Data Import (Task F6).
 *
 * Two-stage flow: PREVIEW first, then an explicit CONFIRM. The file is never
 * applied because it was chosen — only because the user pressed "Confirm
 * import" after seeing what would happen.
 *
 * The chosen file is held in JS memory between the two calls. That is
 * deliberate and bounded: it avoids re-uploading a multi-MB archive, and the
 * selected File reference is dropped as soon as the flow ends (apply, cancel
 * or error) so nothing lingers in the page.
 *
 * Rendering is deliberately bounded too: at most N sample rows per category
 * are drawn, so a 10,000-record import cannot produce an unbounded DOM. Full
 * counts always come from the server response.
 */
(function () {
    "use strict";

    var MAX_SAMPLES = 25;

    var panel = document.getElementById("import-preview-panel");
    var summaryEl = document.getElementById("import-summary");
    var samplesEl = document.getElementById("import-samples");
    var errorEl = document.getElementById("import-error");
    var applyBtn = document.getElementById("import-apply");
    var cancelBtn = document.getElementById("import-cancel");

    var selected = null;   // { file: File, source: "letterboxd"|"tvtime" }
    var csrf = null;

    function readCsrf() {
        if (csrf) { return csrf; }
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) { csrf = meta.getAttribute("content"); }
        return csrf;
    }

    function sayError(message) {
        if (!errorEl) { return; }
        // Restore the assertive semantics. sayDone() re-labels this node as
        // role="status"; without putting it back, a later error would be
        // announced politely and the element would no longer be an alert.
        errorEl.setAttribute("role", "alert");
        errorEl.className = "mt-4 text-sm text-red-400 empty:hidden";
        errorEl.textContent = message || "";
        errorEl.classList.toggle("empty:hidden", !message);
    }

    function reset() {
        selected = null;
        if (panel) { panel.classList.add("hidden"); }
        if (summaryEl) { summaryEl.innerHTML = ""; }
        if (samplesEl) { samplesEl.innerHTML = ""; }
        if (applyBtn) { applyBtn.disabled = true; }
    }

    function fileInputFor(source) {
        return document.querySelector(
            'input[data-import-source="' + source + '"]');
    }

    function labelFor(record) {
        if (record.kind === "episodes") {
            return (record.show_title || "Show") + " S" +
                record.season_number + "E" + record.episode_number;
        }
        var year = record.release_year ? " (" + record.release_year + ")" : "";
        return (record.title || "Untitled") + year;
    }

    var LABELS = {
        imported: "Will import",
        already_present: "Already present",
        unresolved: "Unresolved",
        ambiguous: "Ambiguous",
        ineligible: "Not yet aired",
        invalid: "Malformed rows",
        unsupported: "Unsupported",
        records_detected: "Records found"
    };

    function renderSummary(report) {
        if (!summaryEl) { return; }
        summaryEl.innerHTML = "";
        var keys = Object.keys(LABELS);
        keys.forEach(function (key) {
            var value = report[key];
            if (value === undefined) { return; }
            var wrap = document.createElement("div");
            wrap.className = "bg-gray-900/60 border border-gray-700 rounded p-2";
            var dt = document.createElement("dt");
            dt.className = "text-gray-400 text-xs";
            dt.textContent = LABELS[key];
            var dd = document.createElement("dd");
            dd.className = "text-white text-lg font-semibold";
            dd.textContent = String(value);
            wrap.appendChild(dt);
            wrap.appendChild(dd);
            summaryEl.appendChild(wrap);
        });
    }

    function renderSamples(report) {
        if (!samplesEl) { return; }
        samplesEl.innerHTML = "";
        var groups = [
            { key: "movies", title: "Movies", kind: "movies" },
            { key: "episodes", title: "Episodes", kind: "episodes" },
            { key: "invalid", title: "Malformed rows", kind: "invalid" }
        ];
        var samples = report.samples || {};

        groups.forEach(function (group) {
            var rows = samples[group.key] || [];
            if (!rows.length) { return; }
            var heading = document.createElement("h4");
            heading.className = "text-gray-300 font-medium mt-3 mb-1";
            heading.textContent = group.title + " (" +
                Math.min(rows.length, MAX_SAMPLES) + " shown)";
            samplesEl.appendChild(heading);

            var list = document.createElement("ul");
            list.className = "space-y-1";
            rows.slice(0, MAX_SAMPLES).forEach(function (row) {
                var li = document.createElement("li");
                // Long titles must wrap rather than force horizontal scroll.
                li.className = "text-gray-400 break-words";
                var text;
                if (group.kind === "invalid") {
                    text = "Row " + row.position + ": " + row.reason +
                        (row.excerpt ? " (" + row.excerpt + ")" : "");
                } else {
                    text = labelFor(row) + " — " + row.status +
                        (row.detail ? " (" + row.detail + ")" : "");
                }
                li.textContent = text;
                list.appendChild(li);
            });
            samplesEl.appendChild(list);
        });

        if (!samplesEl.childNodes.length) {
            var empty = document.createElement("p");
            empty.className = "text-gray-500";
            empty.textContent = "Nothing to import from this file.";
            samplesEl.appendChild(empty);
        }
    }

    function post(url, file, done) {
        var form = new FormData();
        form.append("file", file);
        var token = readCsrf();
        if (token) { form.append("csrf_token", token); }

        fetch(url, {
            method: "POST",
            body: form,
            credentials: "same-origin",
            headers: token ? { "X-CSRFToken": token } : {}
        }).then(function (response) {
            return response.json().then(function (body) {
                return { ok: response.ok, status: response.status, body: body };
            }).catch(function () {
                return { ok: false, status: response.status, body: {} };
            });
        }).then(function (result) {
            done(result);
        }).catch(function () {
            sayError("We could not reach the server. Please try again.");
        });
    }

    function startPreview(source) {
        sayError("");
        var input = fileInputFor(source);
        if (!input || !input.files || !input.files.length) {
            sayError("Choose a file first.");
            return;
        }
        var file = input.files[0];
        var button = document.querySelector(
            '[data-action="import-preview"][data-source="' + source + '"]');
        if (button) { button.disabled = true; }

        post("/api/account/import/" + source + "/preview", file,
            function (result) {
                if (button) { button.disabled = false; }
                if (!result.ok) {
                    reset();
                    sayError(result.body.error ||
                        "That file could not be read.");
                    return;
                }
                selected = { file: file, source: source };
                renderSummary(result.body);
                renderSamples(result.body);
                if (panel) { panel.classList.remove("hidden"); }
                if (applyBtn) {
                    applyBtn.disabled = (result.body.imported || 0) === 0;
                }
                if (panel && panel.scrollIntoView) {
                    panel.scrollIntoView({ block: "nearest" });
                }
            });
    }

    function applySelected() {
        if (!selected) { return; }
        sayError("");
        if (applyBtn) { applyBtn.disabled = true; }
        var source = selected.source;
        var file = selected.file;

        post("/api/account/import/" + source + "/apply", file,
            function (result) {
                if (applyBtn) { applyBtn.disabled = false; }
                if (!result.ok) {
                    sayError(result.body.error ||
                        "That import could not be completed.");
                    return;
                }
                var imported = result.body.imported || 0;
                var present = result.body.already_present || 0;
                var unresolved = result.body.unresolved || 0;
                reset();
                var input = fileInputFor(source);
                if (input) { input.value = ""; }
                sayError("");
                sayDone(
                    "Imported " + imported + " record(s). " + present +
                    " already present, " + unresolved + " unresolved.");
            });
    }

    function sayDone(message) {
        if (!errorEl) { return; }
        errorEl.className = "mt-4 text-sm text-green-400";
        errorEl.setAttribute("role", "status");
        errorEl.textContent = message;
    }

    function clearMessage() {
        if (!errorEl) { return; }
        errorEl.setAttribute("role", "alert");
        errorEl.className = "mt-4 text-sm text-red-400 empty:hidden";
        errorEl.textContent = "";
    }

    document.querySelectorAll('[data-action="import-preview"]').forEach(
        function (button) {
            button.addEventListener("click", function () {
                startPreview(button.getAttribute("data-source"));
            });
        });

    if (applyBtn) {
        applyBtn.addEventListener("click", applySelected);
    }
    if (cancelBtn) {
        cancelBtn.addEventListener("click", function () {
            reset();
            clearMessage();
        });
    }
}());