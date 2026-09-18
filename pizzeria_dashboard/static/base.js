(() => {
    "use strict";
    const STORAGE_KEY = "pizzeria-dashboard:viewport-lock";
    const RESTORE_CLASS = "viewport-restore-pending";

    if ("scrollRestoration" in window.history) {
        window.history.scrollRestoration = "manual";
    }

    const reveal = () => document.documentElement.classList.remove(RESTORE_CLASS);
    window.PizzeriaDashboardReveal = reveal;

    try {
        if (window.sessionStorage.getItem(STORAGE_KEY)) {
            document.documentElement.classList.add(RESTORE_CLASS);
        }
    } catch (_error) {
        // The dashboard remains usable when browser storage is unavailable.
    }

    // Never leave a non-dashboard or partially loaded response hidden.
    document.addEventListener("DOMContentLoaded", () => {
        if (!document.getElementById("production-board")) {
            reveal();
        }
    });
    window.setTimeout(reveal, 3_000);
})();
