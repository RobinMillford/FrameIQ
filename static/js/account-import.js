/* Account Data Import (Tasks F6, F7).
 *
 * Two-stage flow: PREVIEW first, then an explicit CONFIRM. The file is never
 * applied because it was chosen — only because the user pressed "Confirm
 * import" after seeing what would happen.
 *
 * The chosen file is held in JS memory between the two calls. That is
 * deliberate and bounded: it avoids re-uploading a multi-MB archive, and the
 * selected File reference is dropped as soon as the flow ends (apply, cancel
 * or error) so nothing lingers in the page. Task F7 needs this more than F6
 * did: unresolved titles are resolved by re-previewing the SAME file with the
 * user's choices attached, which is also what lets the server re-check every
 * choice against the records actually in the file. There is no server-side
 * import session and no upload is persisted.
 *
 * Nothing is auto-selected. A title FrameIQ cannot identify is either chosen
 * explicitly or skipped, because guessing is how an importer quietly corrupts
 * someone's history.
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

    var resolvePanel = document.getElementById("import-resolve-panel");
    var resolveList = document.getElementById("import-resolve-list");
    var resolveStatus = document.getElementById("import-resolve-status");
    var refreshBtn = document.getElementById("import-resolve-refresh");
    var saveMappingsBox = document.getElementById("import-save-mappings");
    var mappingsSection = document.getElementById("import-mappings");
    var mappingsList = document.getElementById("import-mappings-list");

    var selected = null;   // { file: File, source: "letterboxd"|"tvtime" }
    // { resolutionKey: mediaId } — the user's explicit choices. A row absent
    // from this map is a row they have not decided yet, which is reported as
    // unresolved rather than guessed at.
    var selections = {};
    // The most recent preview report, so a choice can re-enable "Confirm"
    // without another round trip.
    var lastReport = null;
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
        selections = {};
        lastReport = null;
        if (panel) { panel.classList.add("hidden"); }
        if (summaryEl) { summaryEl.innerHTML = ""; }
        if (samplesEl) { samplesEl.innerHTML = ""; }
        if (resolvePanel) { resolvePanel.classList.add("hidden"); }
        if (resolveList) { resolveList.innerHTML = ""; }
        if (resolveStatus) { resolveStatus.textContent = ""; }
        if (saveMappingsBox) { saveMappingsBox.checked = false; }
        if (applyBtn) { applyBtn.disabled = true; }
    }

    function sayStatus(message) {
        if (!resolveStatus) { return; }
        resolveStatus.textContent = message || "";
        resolveStatus.classList.toggle("empty:hidden", !message);
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
        conflict: "Kept yours",
        records_detected: "Records found",
        mappings_saved: "Matches saved"
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

    function post(url, file, done, extra) {
        var form = new FormData();
        form.append("file", file);
        if (extra) {
            Object.keys(extra).forEach(function (name) {
                form.append(name, extra[name]);
            });
        }
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

    /* ── Resolution UI (Task F7) ─────────────────────────────────────── */

    function choiceName(key) {
        return "import-choice-" + key;
    }

    function candidateLabel(candidate) {
        var year = candidate.year ? " (" + candidate.year + ")" : "";
        return candidate.title + year;
    }

    function radioRow(key, candidate, groupName) {
        var wrap = document.createElement("div");
        wrap.className = "flex items-start gap-2";
        var input = document.createElement("input");
        input.type = "radio";
        input.name = groupName;
        input.value = String(candidate.media_id);
        input.id = groupName + "-m" + candidate.media_id;
        input.className = "mt-1 border-gray-600 bg-gray-700 text-[var(--accent-hi)] focus:ring-2 focus:ring-[var(--accent-hi)]";
        input.addEventListener("change", onChoiceChanged);
        var label = document.createElement("label");
        label.htmlFor = input.id;
        label.className = "text-sm text-gray-300 break-words cursor-pointer";
        var text = document.createElement("span");
        text.textContent = candidateLabel(candidate);
        if (candidate.url) {
            var link = document.createElement("a");
            link.href = candidate.url;
            link.textContent = "open";
            link.className = "ml-2 text-xs text-[var(--accent-hi)] underline";
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            text.appendChild(link);
        }
        label.appendChild(text);
        wrap.appendChild(input);
        wrap.appendChild(label);
        return wrap;
    }

    function skipRow(key, groupName, isSearch) {
        var wrap = document.createElement("div");
        wrap.className = "flex items-start gap-2";
        var input = document.createElement("input");
        input.type = "radio";
        input.name = groupName;
        // "" means "leave this row out", which the server treats as unresolved.
        input.value = "";
        input.id = groupName + "-skip";
        input.className = "mt-1 border-gray-600 bg-gray-700 text-[var(--accent-hi)] focus:ring-2 focus:ring-[var(--accent-hi)]";
        input.addEventListener("change", onChoiceChanged);
        var label = document.createElement("label");
        label.htmlFor = input.id;
        label.className = "text-sm text-gray-500 break-words cursor-pointer";
        label.textContent = isSearch
            ? "None of these — skip this title"
            : "Skip this title for now";
        wrap.appendChild(input);
        wrap.appendChild(label);
        return wrap;
    }

    function recordKeyOf(row) {
        // Group rows by resolution identity, not by source row: one TV show can
        // appear on forty rows, and the user should answer once, not forty
        // times. The first row carrying each key wins as the group's label.
        return row.resolution_key || null;
    }

    function buildResolveRow(row, kind) {
        var key = recordKeyOf(row);
        if (!key) { return null; }
        var groupName = choiceName(key);

        var fieldset = document.createElement("fieldset");
        fieldset.className = "bg-gray-900/60 border border-gray-700 rounded p-3";
        fieldset.setAttribute("data-resolution-key", key);

        var legend = document.createElement("legend");
        legend.className = "text-white text-sm font-medium break-words px-1";
        legend.textContent = labelFor(Object.assign(
            { kind: kind }, row));
        fieldset.appendChild(legend);

        if (row.detail) {
            var why = document.createElement("p");
            why.className = "text-xs text-gray-400 mb-2 break-words";
            why.textContent = row.detail;
            fieldset.appendChild(why);
        }

        var candidates = row.candidates || [];
        if (candidates.length) {
            candidates.forEach(function (candidate) {
                fieldset.appendChild(radioRow(key, candidate, groupName));
            });
        } else {
            var none = document.createElement("p");
            none.className = "text-xs text-gray-500 mb-2";
            none.textContent = "No similar titles in FrameIQ yet.";
            fieldset.appendChild(none);
        }
        fieldset.appendChild(skipRow(key, groupName, false));

        var searchWrap = document.createElement("div");
        searchWrap.className = "mt-2";
        var searchBtn = document.createElement("button");
        searchBtn.type = "button";
        searchBtn.className = "text-xs text-[var(--accent-hi)] underline focus:outline-none focus:ring-2 focus:ring-[var(--accent-hi)]";
        searchBtn.textContent = candidates.length
            ? "Search FrameIQ for something else"
            : "Search FrameIQ";
        searchBtn.setAttribute("data-action", "import-search");
        searchBtn.addEventListener("click", function () {
            openSearch(row, kind, key, fieldset, searchWrap);
        });
        searchWrap.appendChild(searchBtn);
        fieldset.appendChild(searchWrap);

        return fieldset;
    }

    function onChoiceChanged(event) {
        var fieldset = event.target.closest("fieldset");
        if (!fieldset) { return; }
        var key = fieldset.getAttribute("data-resolution-key");
        if (!key) { return; }
        if (event.target.value === "") {
            delete selections[key];
        } else {
            selections[key] = parseInt(event.target.value, 10);
        }
        updateApplyEnabled();
    }

    function countChoices() {
        return Object.keys(selections).length;
    }

    function updateApplyEnabled() {
        if (!applyBtn || !lastReport) { return; }
        var importable = (lastReport.imported || 0) + countChoices();
        applyBtn.disabled = importable === 0;
    }

    function renderResolve(report) {
        if (!resolveList) { return; }
        resolveList.innerHTML = "";
        var samples = report.samples || {};
        var seen = {};
        var built = 0;

        [["movies", "movies"], ["episodes", "episodes"]].forEach(function (pair) {
            (samples[pair[0]] || []).forEach(function (row) {
                var status = row.status;
                if (status !== "unresolved" && status !== "ambiguous") { return; }
                var key = recordKeyOf(row);
                if (!key || seen[key]) { return; }
                seen[key] = true;
                var node = buildResolveRow(row, pair[1]);
                if (node) { resolveList.appendChild(node); built += 1; }
            });
        });

        if (resolvePanel) {
            resolvePanel.classList.toggle("hidden", built === 0);
        }
        sayStatus(built === 0 ? "" :
            built + " title(s) waiting for a choice.");
    }

    function openSearch(row, kind, key, fieldset, searchWrap) {
        if (searchWrap.dataset.open === "1") { return; }
        searchWrap.dataset.open = "1";

        var form = document.createElement("div");
        form.className = "mt-2 flex flex-col gap-2";

        var input = document.createElement("input");
        input.type = "search";
        input.placeholder = "Search FrameIQ titles";
        input.value = row.title || row.show_title || "";
        input.setAttribute("aria-label", "Search FrameIQ for this title");
        input.className = "w-full text-sm text-gray-200 bg-gray-800 border border-gray-600 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-[var(--accent-hi)]";
        form.appendChild(input);

        var results = document.createElement("div");
        results.className = "space-y-1";
        form.appendChild(results);

        var status = document.createElement("p");
        status.className = "text-xs text-gray-500 empty:hidden";
        status.setAttribute("role", "status");
        form.appendChild(status);

        var groupName = choiceName(key);
        var mediaType = kind === "episodes" ? "tv" : "movie";

        function runSearch() {
            var query = input.value.trim();
            if (!query) { return; }
            status.textContent = "Searching…";
            status.classList.remove("empty:hidden");
            fetch("/api/account/import/" + selected.source + "/search", {
                method: "POST",
                credentials: "same-origin",
                headers: Object.assign(
                    { "Content-Type": "application/json" },
                    readCsrf() ? { "X-CSRFToken": readCsrf() } : {}
                ),
                body: JSON.stringify({ query: query, media_type: mediaType })
            }).then(function (response) {
                return response.json().then(function (body) {
                    return { ok: response.ok, body: body };
                });
            }).then(function (result) {
                results.innerHTML = "";
                if (!result.ok) {
                    status.textContent = result.body.error ||
                        "Search failed.";
                    return;
                }
                var items = result.body.results || [];
                if (!items.length) {
                    status.textContent = "No titles in FrameIQ match \u201c" +
                        query + "\u201d. Add it in FrameIQ first, then re-run " +
                        "the import.";
                    return;
                }
                status.textContent = items.length + " match(es). Pick one:";
                items.forEach(function (candidate) {
                    results.appendChild(
                        radioRow(key, candidate, groupName));
                });
                results.appendChild(skipRow(key, groupName, true));
            }).catch(function () {
                status.textContent = "We could not reach the server.";
            });
        }

        var go = document.createElement("button");
        go.type = "button";
        go.textContent = "Search";
        go.className = "self-start text-xs bg-gray-600 hover:bg-gray-500 text-white px-3 py-1 rounded focus:outline-none focus:ring-2 focus:ring-gray-400";
        go.addEventListener("click", runSearch);
        form.appendChild(go);

        input.addEventListener("keydown", function (event) {
            if (event.key === "Enter") {
                event.preventDefault();
                runSearch();
            }
        });

        searchWrap.appendChild(form);
        input.focus();
    }

    function startPreview(source) {
        sayError("");
        var input = fileInputFor(source);
        if (!input || !input.files || !input.files.length) {
            sayError("Choose a file first.");
            return;
        }
        runPreview(source, input.files[0]);
    }

    /* Re-preview the SAME file with the current choices attached.
     *
     * Choices are not applied optimistically: the server re-resolves and
     * re-classifies, so the counts the user confirms are the counts that will
     * actually be written. A choice that the server rejects surfaces here as a
     * normal error rather than being quietly dropped.
     */
    function runPreview(source, file, done) {
        var button = document.querySelector(
            '[data-action="import-preview"][data-source="' + source + '"]');
        if (button) { button.disabled = true; }
        sayStatus("Updating preview…");

        var extra = { selections: JSON.stringify(selections) };
        post("/api/account/import/" + source + "/preview", file,
            function (result) {
                if (button) { button.disabled = false; }
                if (!result.ok) {
                    if (!selected) { reset(); }
                    sayStatus("");
                    sayError(result.body.error ||
                        "That file could not be read.");
                    if (done) { done(false); }
                    return;
                }
                selected = { file: file, source: source };
                lastReport = result.body;
                renderSummary(result.body);
                renderSamples(result.body);
                renderResolve(result.body);
                if (panel) { panel.classList.remove("hidden"); }
                if (applyBtn) {
                    applyBtn.disabled =
                        (result.body.imported || 0) + countChoices() === 0;
                }
                if (done) { done(true); }
                if (panel && panel.scrollIntoView) {
                    panel.scrollIntoView({ block: "nearest" });
                }
            }, extra);
    }

    function applySelected() {
        if (!selected) { return; }
        sayError("");
        if (applyBtn) { applyBtn.disabled = true; }
        var source = selected.source;
        var file = selected.file;

        var extra = {
            selections: JSON.stringify(selections),
            save_mappings: saveMappingsBox && saveMappingsBox.checked
                ? "1" : "0"
        };

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
                var conflict = result.body.conflict || 0;
                var saved = result.body.mappings_saved || 0;
                reset();
                var input = fileInputFor(source);
                if (input) { input.value = ""; }
                sayError("");
                var message = "Imported " + imported + " record(s). " +
                    present + " already present, " + unresolved +
                    " unresolved.";
                if (conflict) {
                    message += " " + conflict +
                        " kept your own review instead of overwriting it.";
                }
                if (saved) {
                    message += " Saved " + saved + " title match(es).";
                }
                sayDone(message);
                loadMappings();
            }, extra);
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

    /* ── Saved matches (Task F7) ─────────────────────────────────────── */

    var SOURCE_NAMES = { letterboxd: "Letterboxd", tvtime: "TV Time" };

    function mappingRow(mapping) {
        var row = document.createElement("div");
        row.className = "bg-gray-800/40 border border-gray-700 rounded p-3 " +
            "flex flex-wrap items-center gap-2 justify-between";
        row.setAttribute("data-mapping-id", String(mapping.id));

        var text = document.createElement("div");
        text.className = "text-sm min-w-0 break-words";
        var title = document.createElement("span");
        title.className = "text-white";
        title.textContent = mapping.source_title ||
            mapping.source_key || "(untitled)";
        var arrow = document.createElement("span");
        arrow.className = "text-gray-500 mx-1";
        arrow.textContent = "→";
        var target = document.createElement("span");
        target.className = "text-gray-300";
        var targetLabel = mapping.media_title ||
            "a title that no longer exists";
        if (mapping.media_year) {
            targetLabel += " (" + mapping.media_year + ")";
        }
        target.textContent = targetLabel;
        text.appendChild(title);
        text.appendChild(arrow);
        text.appendChild(target);

        var meta = document.createElement("span");
        meta.className = "block text-xs text-gray-500 mt-0.5";
        meta.textContent = (SOURCE_NAMES[mapping.source] || mapping.source) +
            " · " + (mapping.media_type === "tv" ? "TV show" : "Movie");
        if (mapping.stale) {
            meta.className = "block text-xs text-amber-400 mt-0.5";
            meta.textContent += " · needs attention: " +
                (mapping.stale_reason || "no longer valid");
        }
        text.appendChild(meta);
        row.appendChild(text);

        var remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Remove";
        remove.setAttribute("data-action", "import-mapping-delete");
        remove.className = "text-xs bg-gray-700 hover:bg-gray-600 text-white px-3 py-1 rounded focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2 focus:ring-offset-gray-900";
        remove.addEventListener("click", function () {
            deleteMapping(mapping.id, remove);
        });
        row.appendChild(remove);
        return row;
    }

    function loadMappings() {
        if (!mappingsList) { return; }
        fetch("/api/account/import/mappings", {
            credentials: "same-origin",
            headers: readCsrf() ? { "X-CSRFToken": readCsrf() } : {}
        }).then(function (response) {
            return response.json().then(function (body) {
                return { ok: response.ok, body: body };
            });
        }).then(function (result) {
            mappingsList.innerHTML = "";
            var items = (result.body && result.body.mappings) || [];
            if (!items.length) {
                var empty = document.createElement("p");
                empty.className = "text-gray-500 text-sm";
                empty.textContent = "No saved matches yet. Tick " +
                    "\u201cRemember these matches\u201d when importing, or pick a " +
                    "title for an unresolved row.";
                mappingsList.appendChild(empty);
                return;
            }
            items.forEach(function (mapping) {
                mappingsList.appendChild(mappingRow(mapping));
            });
        }).catch(function () {
            // A failed read here must not break the import panel above it.
            mappingsList.innerHTML = "";
            var warn = document.createElement("p");
            warn.className = "text-gray-500 text-sm";
            warn.textContent = "Saved matches could not be loaded.";
            mappingsList.appendChild(warn);
        });
    }

    function deleteMapping(id, button) {
        if (button) { button.disabled = true; }
        fetch("/api/account/import/mappings/" + id, {
            method: "DELETE",
            credentials: "same-origin",
            headers: readCsrf() ? { "X-CSRFToken": readCsrf() } : {}
        }).then(function (response) {
            return response.json().then(function (body) {
                return { ok: response.ok, body: body };
            });
        }).then(function (result) {
            if (!result.ok) {
                if (button) { button.disabled = false; }
                sayError(result.body.error ||
                    "That match could not be removed.");
                return;
            }
            sayError("");
            loadMappings();
        }).catch(function () {
            if (button) { button.disabled = false; }
            sayError("We could not reach the server.");
        });
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
    if (refreshBtn) {
        refreshBtn.addEventListener("click", function () {
            if (!selected) { return; }
            runPreview(selected.source, selected.file);
        });
    }

    loadMappings();
}());