/* Account export UI (Task F5).
 *
 * Both controls are plain GET links to download endpoints, so the browser
 * handles the download natively — no fetch, no blob, no in-page JSON. That is
 * deliberate: rendering export contents into the DOM would put a private
 * account payload into the page, and buffering it in JS would duplicate it in
 * memory for no benefit.
 *
 * This file only adds accessible status feedback so a click that fails is not
 * silent, and marks the link while the server builds the archive.
 */
(function () {
    "use strict";

    var STATUS = document.getElementById("export-status");
    var LINKS = Array.prototype.slice.call(
        document.querySelectorAll("#account-export [data-action]"));

    function say(message) {
        if (!STATUS) { return; }
        STATUS.textContent = message;
        STATUS.classList.toggle("empty:hidden", !message);
    }

    LINKS.forEach(function (link) {
        link.addEventListener("click", function () {
            var isJson = link.getAttribute("data-action") === "export-json";
            say(isJson
                ? "Preparing your JSON export…"
                : "Building your CSV bundle. This can take a moment for a large account.");
            link.setAttribute("aria-busy", "true");
        });
    });
}());