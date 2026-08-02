(() => {
    const dialog = document.getElementById("order-details-dialog");
    const body = document.getElementById("order-details-dialog-body");
    const closeButton = dialog?.querySelector("[data-order-details-close]");

    if (!dialog || !body) {
        return;
    }

    let requestController = null;

    const closeDialog = () => {
        requestController?.abort();
        requestController = null;
        if (dialog.open) {
            if (typeof dialog.close === "function") {
                dialog.close();
            } else {
                dialog.removeAttribute("open");
            }
        }
    };

    const openOrderDetails = async (row) => {
        const url = row.dataset.orderDetailsUrl;
        if (!url) {
            return;
        }

        requestController?.abort();
        requestController = new AbortController();
        body.innerHTML = `
            <div class="order-details-loading" role="status">
                <strong>Loading order details…</strong>
                <span>Loading the cached kitchen view and pickup controls.</span>
            </div>
        `;
        if (!dialog.open) {
            if (typeof dialog.showModal === "function") {
                dialog.showModal();
            } else {
                dialog.setAttribute("open", "");
            }
        }

        try {
            const response = await fetch(url, {
                headers: { "Accept": "text/html" },
                signal: requestController.signal,
            });
            const html = await response.text();
            body.innerHTML = html;
            body.scrollTop = 0;
        } catch (error) {
            if (error.name === "AbortError") {
                return;
            }
            body.innerHTML = `
                <div class="order-details-error" role="alert">
                    <strong>Could not load order details</strong>
                    <p>${String(error)}</p>
                </div>
            `;
        }
    };

    document.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target : null;
        if (
            !target
            || target.closest("[data-bake-timer]")
            || target.closest("[data-oven-position]")
            || target.closest("[data-order-complete-button]")
        ) {
            return;
        }
        const trigger = target.closest("[data-order-details-trigger]");
        const row = (trigger || target).closest("[data-order-details-url]");
        if (row?.dataset.justDragged === "true") {
            return;
        }
        if (row) {
            event.preventDefault();
            openOrderDetails(row);
        }
    });

    document.addEventListener("keydown", (event) => {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || target.closest("[data-bake-timer]") || target.closest("[data-oven-position]")) {
            return;
        }
        if (target.closest("button, a, input, select, textarea")) {
            return;
        }
        const row = target.closest("[data-order-details-url]");
        if (!row || (event.key !== "Enter" && event.key !== " ")) {
            return;
        }
        event.preventDefault();
        openOrderDetails(row);
    });


    document.addEventListener("click", async (event) => {
        const target = event.target instanceof Element ? event.target : null;
        const tab = target?.closest("[data-order-details-tab]");
        if (!tab || !body.contains(tab)) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();

        const tabName = tab.dataset.orderDetailsTab;
        body.querySelectorAll("[data-order-details-tab]").forEach((candidate) => {
            const active = candidate === tab;
            candidate.classList.toggle("is-active", active);
            candidate.setAttribute("aria-selected", active ? "true" : "false");
        });
        body.querySelectorAll("[data-order-details-panel]").forEach((panel) => {
            panel.hidden = panel.dataset.orderDetailsPanel !== tabName;
        });

        if (tabName !== "history") {
            return;
        }
        const historyBody = body.querySelector("[data-customer-history-body]");
        const historyUrl = tab.dataset.customerHistoryUrl;
        if (!historyBody || !historyUrl || historyBody.dataset.loaded === "true") {
            return;
        }

        historyBody.innerHTML = `
            <div class="order-details-loading" role="status">
                <strong>Loading customer history…</strong>
                <span>Looking up linked Square orders in the local history index.</span>
            </div>
        `;
        try {
            const response = await fetch(historyUrl, {headers: {"Accept": "text/html"}});
            const html = await response.text();
            historyBody.innerHTML = html;
            historyBody.dataset.loaded = "true";
        } catch (error) {
            historyBody.innerHTML = `
                <div class="order-details-error" role="alert">
                    <strong>Could not load customer history</strong>
                    <p>${String(error)}</p>
                </div>
            `;
        }
    });


    document.addEventListener("submit", async (event) => {
        const form = event.target.closest("[data-walk-in-assignment-form]");
        if (!form) {
            return;
        }
        event.preventDefault();

        const assignmentUrl = form.dataset.assignmentUrl;
        const status = form.querySelector("[data-walk-in-assignment-status]");
        const submitButton = form.querySelector('button[type="submit"]');
        const formData = new FormData(form);
        if (!assignmentUrl) {
            return;
        }

        submitButton.disabled = true;
        if (status) {
            status.textContent = "Saving…";
        }
        try {
            const response = await fetch(assignmentUrl, {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    service_date: formData.get("service_date"),
                    order_id: formData.get("order_id"),
                    pickup_at: formData.get("pickup_at"),
                }),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) {
                throw new Error(result.error || "The pickup time could not be saved.");
            }
            if (status) {
                status.textContent = "Saved";
            }
            window.location.reload();
        } catch (error) {
            submitButton.disabled = false;
            if (status) {
                status.textContent = String(error);
            }
        }
    });

    closeButton?.addEventListener("click", closeDialog);
    dialog.addEventListener("cancel", (event) => {
        event.preventDefault();
        closeDialog();
    });
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) {
            closeDialog();
        }
    });
})();

(() => {
    document.addEventListener("click", async (event) => {
        const target = event.target instanceof Element ? event.target : null;
        const button = target?.closest("[data-order-complete-button]");
        if (!button) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();
        const url = button.dataset.orderCompleteUrl;
        const serviceDate = button.dataset.serviceDate;
        const orderId = button.dataset.orderId;
        const label = button.dataset.orderLabel || "this order";
        if (!url || !serviceDate || !orderId) {
            return;
        }
        if (!window.confirm(`Mark ${label} completed in Square?`)) {
            return;
        }

        const originalText = button.textContent;
        button.disabled = true;
        button.textContent = "Completing…";
        try {
            const response = await fetch(url, {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    service_date: serviceDate,
                    order_id: orderId,
                }),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) {
                throw new Error(result.error || "Square could not complete the order.");
            }
            button.textContent = "Completed";
            window.location.reload();
        } catch (error) {
            button.disabled = false;
            button.textContent = originalText;
            window.alert(String(error));
        }
    });
})();


(() => {
    const walkIns = Array.from(document.querySelectorAll("[data-walk-in-order-id]"));
    const dropZones = Array.from(document.querySelectorAll("[data-walk-in-drop-zone]"));
    if (!walkIns.length || !dropZones.length) {
        return;
    }

    let activeRow = null;

    const clearDropStates = () => {
        dropZones.forEach((zone) => {
            zone.classList.remove("walk-in-drop-zone--active");
            zone.classList.remove("walk-in-drop-zone--unavailable");
        });
    };

    const zoneAcceptsOrder = (zone, row) => {
        if (!zone.hasAttribute("data-capacity-drop-zone")) {
            return true;
        }
        const openSpaces = Number.parseInt(zone.dataset.openPizzaSpaces || "0", 10);
        const pizzaUnits = Number.parseInt(row.dataset.walkInPizzaUnits || "0", 10);
        return Number.isFinite(openSpaces) && Number.isFinite(pizzaUnits) && openSpaces >= pizzaUnits;
    };

    walkIns.forEach((row) => {
        row.addEventListener("dragstart", (event) => {
            activeRow = row;
            row.classList.add("order-row--dragging");
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", row.dataset.walkInOrderId || "");
            dropZones.forEach((zone) => {
                if (!zoneAcceptsOrder(zone, row)) {
                    zone.classList.add("walk-in-drop-zone--unavailable");
                }
            });
        });

        row.addEventListener("dragend", () => {
            row.classList.remove("order-row--dragging");
            row.dataset.justDragged = "true";
            window.setTimeout(() => delete row.dataset.justDragged, 300);
            activeRow = null;
            clearDropStates();
        });
    });

    dropZones.forEach((zone) => {
        zone.addEventListener("dragover", (event) => {
            if (!activeRow || !zoneAcceptsOrder(zone, activeRow)) {
                return;
            }
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
            zone.classList.add("walk-in-drop-zone--active");
        });

        zone.addEventListener("dragleave", (event) => {
            if (!zone.contains(event.relatedTarget)) {
                zone.classList.remove("walk-in-drop-zone--active");
            }
        });

        zone.addEventListener("drop", async (event) => {
            event.preventDefault();
            const row = activeRow;
            clearDropStates();
            if (!row || !zoneAcceptsOrder(zone, row)) {
                return;
            }

            const assignmentUrl = row.dataset.assignmentUrl;
            const orderId = row.dataset.walkInOrderId;
            const serviceDate = row.dataset.serviceDate;
            if (!assignmentUrl || !orderId || !serviceDate) {
                return;
            }

            document.body.classList.add("walk-in-assignment-pending");
            try {
                const response = await fetch(assignmentUrl, {
                    method: "POST",
                    headers: {
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    body: JSON.stringify({
                        service_date: serviceDate,
                        order_id: orderId,
                        pickup_at: zone.dataset.capacityPickupAt || zone.dataset.pickupAt || "",
                    }),
                });
                const result = await response.json();
                if (!response.ok || !result.ok) {
                    throw new Error(result.error || "The walk-in could not be assigned.");
                }
                window.location.reload();
            } catch (error) {
                document.body.classList.remove("walk-in-assignment-pending");
                window.alert(String(error));
            }
        });
    });
})();


(() => {
    const selectors = Array.from(document.querySelectorAll("[data-oven-position]"));
    if (!selectors.length) {
        return;
    }

    const STORAGE_PREFIX = "pizzeria-dashboard:oven-position:";
    const POSITION_LABELS = {
        "top-left": "Top left",
        "top-right": "Top right",
        "bottom-left": "Bottom left",
        "bottom-right": "Bottom right",
    };

    const selectorsByDate = new Map();
    selectors.forEach((selector) => {
        const serviceDate = selector.dataset.serviceDate || "unknown";
        if (!selectorsByDate.has(serviceDate)) {
            selectorsByDate.set(serviceDate, []);
        }
        selectorsByDate.get(serviceDate).push(selector);
    });

    const storageKeyFor = (serviceDate) => `${STORAGE_PREFIX}${serviceDate}`;

    const readState = (serviceDate) => {
        try {
            const raw = window.localStorage.getItem(storageKeyFor(serviceDate));
            const parsed = raw ? JSON.parse(raw) : {};
            return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
        } catch (_error) {
            return {};
        }
    };

    const writeState = (serviceDate, state) => {
        try {
            if (Object.keys(state).length) {
                window.localStorage.setItem(storageKeyFor(serviceDate), JSON.stringify(state));
            } else {
                window.localStorage.removeItem(storageKeyFor(serviceDate));
            }
        } catch (_error) {
            // Oven tracking still works for the current page when storage is unavailable.
        }
    };

    const states = new Map();

    const renderDate = (serviceDate) => {
        const dateSelectors = selectorsByDate.get(serviceDate) || [];
        const state = states.get(serviceDate) || {};

        dateSelectors.forEach((selector) => {
            const pieKey = selector.dataset.ovenPositionKey;
            let selectedPosition = null;

            selector.querySelectorAll("[data-oven-position-choice]").forEach((button) => {
                const position = button.dataset.ovenPositionChoice;
                const occupant = state[position];
                const selected = occupant === pieKey;
                const occupiedByAnother = Boolean(occupant) && !selected;
                const label = POSITION_LABELS[position] || position;

                if (selected) {
                    selectedPosition = position;
                }
                button.classList.toggle("is-selected", selected);
                button.classList.toggle("is-occupied", occupiedByAnother);
                button.setAttribute("aria-pressed", selected ? "true" : "false");
                button.dataset.occupied = occupant ? "true" : "false";
                button.title = selected
                    ? `${label} · this pie`
                    : occupiedByAnother
                        ? `${label} · occupied by another pie`
                        : `${label} · available`;
            });

            selector.classList.toggle("oven-position-selector--assigned", Boolean(selectedPosition));
            selector.dataset.selectedOvenPosition = selectedPosition || "";
        });
    };

    selectorsByDate.forEach((dateSelectors, serviceDate) => {
        const knownPieKeys = new Set(dateSelectors.map((selector) => selector.dataset.ovenPositionKey));
        const state = readState(serviceDate);
        Object.keys(state).forEach((position) => {
            if (!knownPieKeys.has(state[position])) {
                delete state[position];
            }
        });
        states.set(serviceDate, state);
        writeState(serviceDate, state);
        renderDate(serviceDate);
    });

    selectors.forEach((selector) => {
        selector.addEventListener("dragstart", (event) => event.preventDefault());
        selector.querySelectorAll("[data-oven-position-choice]").forEach((button) => {
            button.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();

                const serviceDate = selector.dataset.serviceDate || "unknown";
                const pieKey = selector.dataset.ovenPositionKey;
                const position = button.dataset.ovenPositionChoice;
                const state = states.get(serviceDate) || {};

                if (state[position] === pieKey) {
                    delete state[position];
                } else {
                    Object.keys(state).forEach((candidate) => {
                        if (state[candidate] === pieKey) {
                            delete state[candidate];
                        }
                    });
                    state[position] = pieKey;
                }

                states.set(serviceDate, state);
                writeState(serviceDate, state);
                renderDate(serviceDate);
            });
        });
    });
})();

(() => {
    const timers = Array.from(document.querySelectorAll("[data-bake-timer]"));
    if (!timers.length) {
        return;
    }

    const STORAGE_PREFIX = "pizzeria-dashboard:bake-timer:";
    const DEFAULT_DURATION_SECONDS = 8 * 60;

    const safeRead = (key) => {
        try {
            const raw = window.localStorage.getItem(key);
            return raw ? JSON.parse(raw) : null;
        } catch (_error) {
            return null;
        }
    };

    const safeWrite = (key, value) => {
        try {
            window.localStorage.setItem(key, JSON.stringify(value));
        } catch (_error) {
            // The timer still works for the current page if storage is unavailable.
        }
    };

    const safeRemove = (key) => {
        try {
            window.localStorage.removeItem(key);
        } catch (_error) {
            // Nothing else to do.
        }
    };

    const formatRemaining = (milliseconds) => {
        const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        return `${minutes}:${String(seconds).padStart(2, "0")}`;
    };

    const durationFor = (timer) => {
        const seconds = Number.parseInt(timer.dataset.bakeDurationSeconds || "", 10);
        return (Number.isFinite(seconds) ? seconds : DEFAULT_DURATION_SECONDS) * 1000;
    };

    const storageKeyFor = (timer) => `${STORAGE_PREFIX}${timer.dataset.bakeTimerKey}`;

    const defaultState = (timer) => ({
        status: "idle",
        remainingMs: durationFor(timer),
        endAt: null,
    });

    const normalizedState = (timer) => {
        const state = safeRead(storageKeyFor(timer));
        if (!state || !["running", "paused", "done"].includes(state.status)) {
            return defaultState(timer);
        }

        if (state.status === "running") {
            const endAt = Number(state.endAt);
            if (!Number.isFinite(endAt)) {
                return defaultState(timer);
            }
            if (endAt <= Date.now()) {
                return { status: "done", remainingMs: 0, endAt: null };
            }
            return { status: "running", remainingMs: endAt - Date.now(), endAt };
        }

        if (state.status === "paused") {
            const remainingMs = Number(state.remainingMs);
            return {
                status: "paused",
                remainingMs: Number.isFinite(remainingMs) ? Math.max(0, remainingMs) : durationFor(timer),
                endAt: null,
            };
        }

        return { status: "done", remainingMs: 0, endAt: null };
    };

    const saveState = (timer, state) => {
        timer._bakeTimerState = state;
        if (state.status === "idle") {
            safeRemove(storageKeyFor(timer));
        } else {
            safeWrite(storageKeyFor(timer), state);
        }
    };

    const render = (timer, now = Date.now()) => {
        const action = timer.querySelector("[data-bake-timer-action]");
        const display = timer.querySelector("[data-bake-timer-display]");
        const toggle = timer.querySelector("[data-bake-timer-toggle]");
        let state = timer._bakeTimerState || normalizedState(timer);

        if (state.status === "running") {
            const remainingMs = Math.max(0, Number(state.endAt) - now);
            if (remainingMs <= 0) {
                state = { status: "done", remainingMs: 0, endAt: null };
                saveState(timer, state);
            } else {
                state = { ...state, remainingMs };
                timer._bakeTimerState = state;
            }
        }

        timer.classList.toggle("bake-timer--running", state.status === "running");
        timer.classList.toggle("bake-timer--paused", state.status === "paused");
        timer.classList.toggle("bake-timer--done", state.status === "done");

        if (state.status === "running") {
            action.textContent = "Pause";
            display.textContent = formatRemaining(state.remainingMs);
            toggle.disabled = false;
        } else if (state.status === "paused") {
            action.textContent = "Resume";
            display.textContent = formatRemaining(state.remainingMs);
            toggle.disabled = false;
        } else if (state.status === "done") {
            action.textContent = "Finished";
            display.textContent = "DONE";
            toggle.disabled = true;
        } else {
            action.textContent = "Start";
            display.textContent = formatRemaining(durationFor(timer));
            toggle.disabled = false;
        }
    };

    const start = (timer, remainingMs = durationFor(timer)) => {
        const state = {
            status: "running",
            remainingMs,
            endAt: Date.now() + remainingMs,
        };
        saveState(timer, state);
        render(timer);
    };

    const pause = (timer) => {
        const state = timer._bakeTimerState;
        const remainingMs = Math.max(0, Number(state.endAt) - Date.now());
        saveState(timer, { status: "paused", remainingMs, endAt: null });
        render(timer);
    };

    const reset = (timer) => {
        saveState(timer, defaultState(timer));
        render(timer);
    };

    timers.forEach((timer) => {
        timer._bakeTimerState = normalizedState(timer);
        render(timer);

        timer.querySelector("[data-bake-timer-toggle]")?.addEventListener("click", (event) => {
            event.stopPropagation();
            const state = timer._bakeTimerState;
            if (state.status === "running") {
                pause(timer);
            } else if (state.status === "paused") {
                start(timer, state.remainingMs);
            } else if (state.status === "idle") {
                start(timer);
            }
        });

        timer.querySelector("[data-bake-timer-reset]")?.addEventListener("click", (event) => {
            event.stopPropagation();
            reset(timer);
        });
    });

    window.setInterval(() => {
        const now = Date.now();
        timers.forEach((timer) => render(timer, now));
    }, 250);
})();

(() => {
    const board = document.getElementById("production-board");
    if (!board || board.dataset.autoSyncAvailable !== "true") {
        return;
    }

    const syncUrl = board.dataset.autoSyncUrl;
    const serviceDate = board.dataset.serviceDate;
    const status = document.querySelector("[data-auto-sync-status]");
    const incrementalButton = document.querySelector("[data-incremental-sync-button]");
    const controls = document.querySelector("[data-auto-refresh-controls]");
    const toggle = controls?.querySelector("[data-auto-sync-toggle]");
    const intervalSelect = controls?.querySelector("[data-auto-sync-interval]");
    const settingsUrl = controls?.dataset.settingsUrl;
    if (!syncUrl || !serviceDate) {
        return;
    }

    let enabled = board.dataset.autoSyncEnabled === "true";
    let seconds = Math.max(
        10,
        Number.parseInt(intervalSelect?.value || board.dataset.autoSyncSeconds || "30", 10),
    );
    let syncing = false;
    let timerId = null;

    const setStatus = (message) => {
        if (status) {
            status.textContent = message;
        }
    };

    const setButtonState = (busy) => {
        if (!incrementalButton) {
            return;
        }
        incrementalButton.disabled = busy;
        incrementalButton.textContent = busy ? "Checking Square…" : "Incremental update";
    };

    const schedule = (delaySeconds = seconds) => {
        window.clearTimeout(timerId);
        timerId = null;
        if (!enabled) {
            setStatus("Auto refresh stopped");
            return;
        }
        timerId = window.setTimeout(() => runQuickSync(false), delaySeconds * 1000);
    };

    const shouldDefer = () => (
        document.hidden
        || Boolean(document.querySelector("dialog[open]"))
        || document.body.classList.contains("walk-in-assignment-pending")
    );

    const savePreferences = async () => {
        if (!settingsUrl) {
            return;
        }
        try {
            const response = await fetch(settingsUrl, {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({enabled, seconds}),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) {
                throw new Error(result.error || "Could not save refresh settings.");
            }
        } catch (error) {
            setStatus(`Settings not saved · ${String(error)}`);
        }
    };

    const runQuickSync = async (manual = false) => {
        if (syncing) {
            return;
        }
        if (!manual && shouldDefer()) {
            schedule(5);
            return;
        }

        syncing = true;
        setButtonState(true);
        setStatus(manual ? "Running incremental update…" : "Checking Square…");
        try {
            const response = await fetch(syncUrl, {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({service_date: serviceDate}),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) {
                throw new Error(result.error || "Square refresh failed.");
            }

            const orderChanges = Number(result.changed_count || 0) + Number(result.removed_count || 0);
            const historyChanges = Number(result.customer_history_changed || 0);
            const changes = orderChanges + historyChanges;
            if (changes > 0) {
                setStatus(`${changes} dashboard change${changes === 1 ? "" : "s"} found—updating…`);
                window.location.reload();
                return;
            }

            const now = new Date();
            const checkedAt = now.toLocaleTimeString([], {
                hour: "numeric",
                minute: "2-digit",
                second: "2-digit",
            });
            setStatus(`${enabled ? "Live" : "Incremental update complete"} · checked ${checkedAt}`);
        } catch (error) {
            setStatus(`Refresh paused · ${String(error)}`);
        } finally {
            syncing = false;
            setButtonState(false);
            schedule();
        }
    };

    incrementalButton?.addEventListener("click", () => runQuickSync(true));

    toggle?.addEventListener("change", () => {
        enabled = toggle.checked;
        savePreferences();
        if (enabled) {
            setStatus(`Auto refresh enabled · every ${seconds}s`);
            schedule(1);
        } else {
            window.clearTimeout(timerId);
            timerId = null;
            setStatus("Auto refresh stopped");
        }
    });

    intervalSelect?.addEventListener("change", () => {
        seconds = Math.max(10, Number.parseInt(intervalSelect.value || "30", 10));
        savePreferences();
        setStatus(enabled ? `Auto refresh set to every ${seconds}s` : "Auto refresh stopped");
        schedule();
    });

    document.addEventListener("visibilitychange", () => {
        if (!document.hidden && enabled) {
            schedule(1);
        }
    });

    if (enabled) {
        schedule(3);
    } else {
        setStatus("Auto refresh stopped");
    }
})();

(() => {
    document.querySelectorAll("[data-full-sync-form]").forEach((form) => {
        form.addEventListener("submit", () => {
            const button = form.querySelector("[data-full-sync-button]");
            if (!button) {
                return;
            }
            button.disabled = true;
            button.textContent = "Full refresh running…";
        });
    });
})();


(() => {
    document.querySelectorAll("[data-customer-history-form]").forEach((form) => {
        form.addEventListener("submit", () => {
            const button = form.querySelector("[data-customer-history-button]");
            if (!button) {
                return;
            }
            button.disabled = true;
            button.textContent = "Building history…";
        });
    });
})();

(() => {
    const dialog = document.querySelector("#service-setup-dialog");
    const openButton = document.querySelector("[data-service-setup-open]");
    const closeButton = dialog?.querySelector("[data-service-setup-close]");
    if (!dialog || !openButton) {
        return;
    }

    const openDialog = () => {
        if (typeof dialog.showModal === "function") {
            dialog.showModal();
        } else {
            dialog.setAttribute("open", "");
        }
        const firstInput = dialog.querySelector("input, textarea, select, button");
        firstInput?.focus();
    };

    const closeDialog = () => {
        if (typeof dialog.close === "function") {
            dialog.close();
        } else {
            dialog.removeAttribute("open");
        }
        openButton.focus();
    };

    openButton.addEventListener("click", openDialog);
    closeButton?.addEventListener("click", closeDialog);
    dialog.addEventListener("cancel", (event) => {
        event.preventDefault();
        closeDialog();
    });
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) {
            closeDialog();
        }
    });
})();
