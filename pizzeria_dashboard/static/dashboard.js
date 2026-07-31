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
                <strong>Loading Square order details…</strong>
                <span>The cached order will remain available if Square cannot be reached.</span>
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
        if (!target || target.closest("[data-bake-timer]")) {
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
        if (!target || target.closest("[data-bake-timer]")) {
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
        const debugTab = target?.closest("[data-order-debug-url]");
        if (!debugTab) return;
        event.preventDefault();
        const content = debugTab.closest(".order-details-content");
        const pickupPanel = content?.querySelector('[data-order-panel="pickup"]');
        const debugPanel = content?.querySelector("[data-order-debug-panel]");
        content?.querySelectorAll(".order-detail-tab").forEach((tab) => tab.classList.remove("is-active"));
        debugTab.classList.add("is-active");
        if (pickupPanel) pickupPanel.hidden = true;
        if (debugPanel) debugPanel.hidden = false;
        if (!debugPanel || debugPanel.dataset.loaded === "true") return;
        debugPanel.innerHTML = '<div class="order-details-loading"><strong>Loading Square debug data…</strong></div>';
        try {
            const response = await fetch(debugTab.dataset.orderDebugUrl, {headers: {Accept: "text/html"}});
            debugPanel.innerHTML = await response.text();
            debugPanel.dataset.loaded = "true";
        } catch (error) {
            debugPanel.innerHTML = `<div class="order-details-error"><strong>Could not load debug data</strong><p>${String(error)}</p></div>`;
        }
    });

    document.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target : null;
        const pickupTab = target?.closest('[data-order-tab="pickup"]');
        if (!pickupTab) return;
        const content = pickupTab.closest(".order-details-content");
        content?.querySelectorAll(".order-detail-tab").forEach((tab) => tab.classList.remove("is-active"));
        pickupTab.classList.add("is-active");
        const pickupPanel = content?.querySelector('[data-order-panel="pickup"]');
        const debugPanel = content?.querySelector("[data-order-debug-panel]");
        if (pickupPanel) pickupPanel.hidden = false;
        if (debugPanel) debugPanel.hidden = true;
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
