(() => {
    const STORAGE_KEY = "pizzeria-dashboard:viewport-lock";

    if ("scrollRestoration" in window.history) {
        window.history.scrollRestoration = "manual";
    }

    const visible = (element) => {
        if (!element || element.hidden) {
            return false;
        }
        const rect = element.getBoundingClientRect();
        return rect.bottom > 0 && rect.top < window.innerHeight;
    };

    const nearestVisible = (elements) => {
        const candidates = Array.from(elements)
            .filter(visible)
            .map((element) => ({element, rect: element.getBoundingClientRect()}));
        if (!candidates.length) {
            return null;
        }
        return candidates.sort((left, right) => {
            const leftTop = left.rect.top >= 0 ? left.rect.top : Math.abs(left.rect.top) + 10000;
            const rightTop = right.rect.top >= 0 ? right.rect.top : Math.abs(right.rect.top) + 10000;
            return leftTop - rightTop;
        })[0].element;
    };

    const parentSlotPickupAt = (element) => (
        element?.closest?.(".pickup-window[data-pickup-at]")?.dataset.pickupAt || ""
    );

    const capture = () => {
        const allVisibleTimers = Array.from(document.querySelectorAll("[data-bake-timer][data-bake-timer-key]"))
            .filter(visible);
        const activeVisibleTimers = allVisibleTimers.filter((timer) => (
            timer.dataset.timerStatus === "running"
            || timer.dataset.timerStatus === "paused"
            || timer.dataset.timerStatus === "done"
        ));
        const timer = nearestVisible(activeVisibleTimers.length ? activeVisibleTimers : allVisibleTimers);
        const order = timer?.closest(".order-row[data-order-id]")
            || nearestVisible(document.querySelectorAll("#production-board .order-row[data-order-id]"));
        const slot = order?.closest(".pickup-window[data-pickup-at]")
            || timer?.closest(".pickup-window[data-pickup-at]")
            || nearestVisible(document.querySelectorAll(".pickup-window[data-pickup-at]:not([hidden])"));
        const anchor = timer || order || slot;
        const rect = anchor?.getBoundingClientRect();

        return {
            timerKey: timer?.dataset.bakeTimerKey || "",
            orderId: order?.dataset.orderId || "",
            pickupAt: slot?.dataset.pickupAt || parentSlotPickupAt(anchor),
            top: rect ? rect.top : null,
            scrollY: window.scrollY,
        };
    };

    const elementFor = (snapshot) => {
        if (!snapshot) {
            return null;
        }
        if (snapshot.timerKey) {
            const timer = Array.from(document.querySelectorAll("[data-bake-timer][data-bake-timer-key]"))
                .find((element) => element.dataset.bakeTimerKey === snapshot.timerKey);
            if (timer && !timer.hidden) {
                return timer;
            }
        }
        if (snapshot.orderId) {
            const order = Array.from(document.querySelectorAll("#production-board .order-row[data-order-id]"))
                .find((element) => element.dataset.orderId === snapshot.orderId);
            if (order && !order.closest(".pickup-window[hidden]")) {
                return order;
            }
        }
        if (snapshot.pickupAt) {
            const slots = Array.from(document.querySelectorAll(".pickup-window[data-pickup-at]:not([hidden])"));
            return slots.find((element) => element.dataset.pickupAt === snapshot.pickupAt)
                || slots.find((element) => (element.dataset.pickupAt || "") > snapshot.pickupAt)
                || null;
        }
        return null;
    };

    const restore = (snapshot, {defer = true} = {}) => {
        if (!snapshot) {
            return;
        }
        const apply = () => {
            const anchor = elementFor(snapshot);
            const savedTop = Number(snapshot.top);
            if (anchor && Number.isFinite(savedTop)) {
                const delta = anchor.getBoundingClientRect().top - savedTop;
                if (Math.abs(delta) > 0.5) {
                    window.scrollTo(0, Math.max(0, window.scrollY + delta));
                }
                return;
            }
            const savedScrollY = Number(snapshot.scrollY);
            if (Number.isFinite(savedScrollY)) {
                window.scrollTo(0, Math.max(0, savedScrollY));
            }
        };

        if (!defer) {
            apply();
            return;
        }
        window.requestAnimationFrame(() => window.requestAnimationFrame(apply));
    };

    const remember = () => {
        try {
            window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(capture()));
        } catch (_error) {
            // A reload can still proceed if browser storage is unavailable.
        }
    };

    const restoreSaved = () => {
        let snapshot = null;
        try {
            snapshot = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) || "null");
            window.sessionStorage.removeItem(STORAGE_KEY);
        } catch (_error) {
            snapshot = null;
        }
        restore(snapshot, {defer: false});
    };

    const preserve = (mutation) => {
        const snapshot = capture();
        mutation();
        restore(snapshot, {defer: false});
    };

    window.PizzeriaDashboardViewport = {capture, remember, preserve, restore, restoreSaved};
})();

(() => {
    const board = document.getElementById("production-board");
    const control = document.querySelector("[data-prep-view-control]");
    const toggle = control?.querySelector("[data-prep-view-toggle]");
    const slots = Array.from(document.querySelectorAll(".pickup-window[data-pickup-at]"));
    const emptyState = document.querySelector("[data-prep-view-empty]");
    if (!board || !control || !toggle || !slots.length) {
        return;
    }

    const storageKey = "pizzeria-dashboard:prep-view";

    const savedPreference = () => {
        try {
            return window.localStorage.getItem(storageKey) === "true";
        } catch (_error) {
            return false;
        }
    };

    const savePreference = (enabled) => {
        try {
            window.localStorage.setItem(storageKey, enabled ? "true" : "false");
        } catch (_error) {
            // The view still works when browser storage is disabled.
        }
    };

    const applyPrepView = () => {
        const enabled = toggle.checked;
        let visibleSlots = 0;
        slots.forEach((slot) => {
            // Past slots deliberately stay in place. Removing them during service
            // shifts the board underneath active timers and oven controls.
            const hidden = enabled && slot.dataset.slotEmpty === "true";
            slot.hidden = hidden;
            if (!hidden) {
                visibleSlots += 1;
            }
        });

        control.classList.toggle("prep-view-control--active", enabled);
        control.setAttribute("aria-label", enabled
            ? "Prep view on. Empty slots are hidden; past orders stay visible."
            : "Prep view off. All service slots are shown.");
        if (emptyState) {
            emptyState.hidden = !enabled || visibleSlots > 0;
        }
    };

    toggle.checked = savedPreference();
    applyPrepView();
    window.PizzeriaDashboardViewport?.restoreSaved();

    toggle.addEventListener("change", () => {
        savePreference(toggle.checked);
        window.PizzeriaDashboardViewport?.preserve(applyPrepView);
    });
})();

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
            || target.closest("[data-order-boxed-button]")
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


    document.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target : null;
        const clearButton = target?.closest("[data-order-note-clear]");
        if (!clearButton || !body.contains(clearButton)) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        const form = clearButton.closest("[data-order-note-form]");
        const textarea = form?.querySelector('textarea[name="note"]');
        if (!form || !textarea) {
            return;
        }
        textarea.value = "";
        form.requestSubmit();
    });

    document.addEventListener("submit", async (event) => {
        const form = event.target.closest("[data-order-note-form]");
        if (!form) {
            return;
        }
        event.preventDefault();

        const noteUrl = form.dataset.orderNoteUrl;
        const status = form.querySelector("[data-order-note-status]");
        const submitButton = form.querySelector('button[type="submit"]');
        const clearButton = form.querySelector("[data-order-note-clear]");
        const formData = new FormData(form);
        if (!noteUrl || !submitButton) {
            return;
        }

        submitButton.disabled = true;
        if (clearButton) {
            clearButton.disabled = true;
        }
        if (status) {
            status.textContent = "Saving…";
        }
        try {
            const response = await fetch(noteUrl, {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    service_date: formData.get("service_date"),
                    order_id: formData.get("order_id"),
                    note: formData.get("note"),
                }),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) {
                throw new Error(result.error || "The staff note could not be saved.");
            }
            if (status) {
                status.textContent = result.note ? "Saved" : "Cleared";
            }
            window.PizzeriaDashboardViewport?.remember();
            window.location.reload();
        } catch (error) {
            submitButton.disabled = false;
            if (clearButton) {
                clearButton.disabled = false;
            }
            if (status) {
                status.textContent = String(error);
            }
        }
    });

    document.addEventListener("submit", async (event) => {
        const form = event.target.closest("[data-scheduled-pickup-time-form]");
        if (!form) {
            return;
        }
        event.preventDefault();

        const pickupTimeUrl = form.dataset.pickupTimeUrl;
        const status = form.querySelector("[data-scheduled-pickup-time-status]");
        const submitButton = form.querySelector('button[type="submit"]');
        const formData = new FormData(form);
        if (!pickupTimeUrl || !submitButton) {
            return;
        }

        submitButton.disabled = true;
        if (status) {
            status.textContent = "Saving…";
        }
        try {
            const response = await fetch(pickupTimeUrl, {
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
                status.textContent = result.overridden ? "Adjusted" : "Original time restored";
            }
            window.PizzeriaDashboardViewport?.remember();
            window.location.reload();
        } catch (error) {
            submitButton.disabled = false;
            if (status) {
                status.textContent = String(error);
            }
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
            window.PizzeriaDashboardViewport?.remember();
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
            window.PizzeriaDashboardViewport?.remember();
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
                window.PizzeriaDashboardViewport?.remember();
                window.location.reload();
            } catch (error) {
                document.body.classList.remove("walk-in-assignment-pending");
                window.alert(String(error));
            }
        });
    });
})();


(() => {
    const board = document.getElementById("production-board");
    const region = document.querySelector("[data-new-order-toast-region]");
    const rows = Array.from(document.querySelectorAll(".order-row[data-order-id]"));
    const serviceDate = board?.dataset.serviceDate;
    if (!board || !region || !serviceDate) {
        return;
    }

    const rowsById = new Map(rows.map((row) => [row.dataset.orderId, row]));
    const currentIds = Array.from(rowsById.keys());
    const knownStorageKey = `pizzeria-dashboard:known-orders:${serviceDate}`;
    const pendingStorageKey = `pizzeria-dashboard:pending-order-toasts:${serviceDate}`;
    const dismissedStorageKey = `pizzeria-dashboard:dismissed-order-toasts:${serviceDate}`;

    const loadStringSet = (storage, key) => {
        try {
            const raw = storage.getItem(key);
            const parsed = raw ? JSON.parse(raw) : [];
            return new Set(Array.isArray(parsed) ? parsed.map(String) : []);
        } catch (_error) {
            return new Set();
        }
    };
    const saveStringSet = (storage, key, values) => {
        try {
            storage.setItem(key, JSON.stringify(Array.from(values)));
        } catch (_error) {
            // Device-local alert persistence is helpful but not required for service.
        }
    };

    let knownIds = null;
    try {
        const raw = window.sessionStorage.getItem(knownStorageKey);
        if (raw) {
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed)) {
                knownIds = new Set(parsed.map(String));
            }
        }
        window.sessionStorage.setItem(knownStorageKey, JSON.stringify(currentIds));
    } catch (_error) {
        return;
    }

    const pendingIds = loadStringSet(window.localStorage, pendingStorageKey);
    const dismissedIds = loadStringSet(window.localStorage, dismissedStorageKey);

    // The first load establishes a device-local baseline. After that, newly seen
    // orders become persistent alerts on this browser until this browser dismisses
    // them. Prep and expo therefore manage their alerts independently.
    if (knownIds) {
        currentIds
            .filter((orderId) => !knownIds.has(orderId) && !dismissedIds.has(orderId))
            .forEach((orderId) => pendingIds.add(orderId));
    }
    Array.from(pendingIds)
        .filter((orderId) => !rowsById.has(orderId))
        .forEach((orderId) => pendingIds.delete(orderId));
    saveStringSet(window.localStorage, pendingStorageKey, pendingIds);

    const summarizeItems = (row) => {
        const labels = Array.from(row.querySelectorAll(".item-row .item-label"))
            .map((label) => label.textContent.replace(/\s+/g, " ").trim())
            .filter(Boolean);
        if (labels.length <= 3) {
            return labels.join(" · ");
        }
        return `${labels.slice(0, 3).join(" · ")} · +${labels.length - 3} more`;
    };

    const dismissOrder = (orderId) => {
        pendingIds.delete(orderId);
        dismissedIds.add(orderId);
        saveStringSet(window.localStorage, pendingStorageKey, pendingIds);
        saveStringSet(window.localStorage, dismissedStorageKey, dismissedIds);
        renderToasts();
    };

    const createToast = (row) => {
        const orderId = row.dataset.orderId;
        const toast = document.createElement("div");
        toast.className = "new-order-toast";
        toast.dataset.orderId = orderId;
        toast.setAttribute("aria-label", "New order alert. Click to dismiss.");
        const name = row.querySelector(".customer-name")?.textContent.trim() || "New order";
        const pickup = row.dataset.orderPickupLabel || "Pickup time unavailable";
        const summary = summarizeItems(row) || "Production order";

        const heading = document.createElement("div");
        heading.className = "new-order-toast-heading";
        const title = document.createElement("strong");
        title.textContent = `NEW ORDER · ${pickup}`;
        const close = document.createElement("button");
        close.type = "button";
        close.setAttribute("aria-label", "Dismiss new order alert");
        close.textContent = "×";
        heading.append(title, close);

        const customer = document.createElement("span");
        customer.textContent = name;
        const items = document.createElement("small");
        items.textContent = summary;
        const hint = document.createElement("small");
        hint.className = "new-order-toast-hint";
        hint.textContent = "Click to dismiss";
        toast.append(heading, customer, items, hint);

        const dismiss = (event) => {
            event?.preventDefault();
            event?.stopPropagation();
            dismissOrder(orderId);
        };
        close.addEventListener("click", dismiss);
        toast.addEventListener("click", dismiss);
        return toast;
    };

    function renderToasts() {
        region.replaceChildren();
        Array.from(pendingIds)
            .filter((orderId) => !dismissedIds.has(orderId))
            .map((orderId) => rowsById.get(orderId))
            .filter(Boolean)
            .slice(-4)
            .forEach((row) => region.appendChild(createToast(row)));
    }

    renderToasts();
})();

(() => {
    const board = document.getElementById("production-board");
    const timers = Array.from(document.querySelectorAll("[data-bake-timer]"));
    const selectors = Array.from(document.querySelectorAll("[data-oven-position]"));
    const orderRows = Array.from(document.querySelectorAll(".order-row[data-order-id]"));
    const countdown = document.querySelector("[data-pizza-countdown]");
    const decrementAllDayCounts = board?.dataset.decrementAllDayCounts === "true";
    const pieAllDayRows = Array.from(document.querySelectorAll("[data-pie-all-day-row]"));
    const modifierAllDayRows = Array.from(document.querySelectorAll("[data-modifier-all-day-row]"));
    const pieAllDayTotal = document.querySelector("[data-pie-all-day-total]");
    const modifierAllDayTotal = document.querySelector("[data-modifier-all-day-total]");
    if (!board || (!timers.length && !selectors.length && !orderRows.length)) {
        return;
    }

    const stateUrl = board.dataset.liveProductionStateUrl;
    const updateUrl = board.dataset.pieProductionStateUrl;
    const orderReadyUrl = board.dataset.orderReadyUrl;
    const serviceDate = board.dataset.serviceDate;
    const serviceTimezone = board.dataset.serviceTimezone || "America/New_York";
    if (!stateUrl || !updateUrl || !orderReadyUrl || !serviceDate) {
        return;
    }

    const POSITION_LABELS = {
        "top-left": "Top left",
        "top-right": "Top right",
        "bottom-left": "Bottom left",
        "bottom-right": "Bottom right",
    };
    const DEFAULT_DURATION_MS = 8 * 60 * 1000;
    const timersByKey = new Map(timers.map((timer) => [timer.dataset.bakeTimerKey, timer]));
    const selectorsByKey = new Map(selectors.map((selector) => [selector.dataset.ovenPositionKey, selector]));
    const rowsByOrderId = new Map(orderRows.map((row) => [row.dataset.orderId, row]));
    let serverOffsetMs = 0;
    let polling = false;
    let pieStates = {};
    let boxedOrders = {};

    const initialPieState = (key) => {
        const timer = timersByKey.get(key);
        const selector = selectorsByKey.get(key);
        return {
            timer_status: timer?.dataset.timerStatus || "idle",
            timer_remaining_ms: Number(timer?.dataset.timerRemainingMs || DEFAULT_DURATION_MS),
            timer_end_at_ms: Number(timer?.dataset.timerEndAtMs || 0) || null,
            oven_position: selector?.dataset.selectedOvenPosition || null,
        };
    };
    new Set([...timersByKey.keys(), ...selectorsByKey.keys()]).forEach((key) => {
        pieStates[key] = initialPieState(key);
    });
    orderRows.forEach((row) => {
        if (row.dataset.boxedAt) {
            boxedOrders[row.dataset.orderId] = row.dataset.boxedAt;
        }
    });

    const adjustedNow = () => Date.now() + serverOffsetMs;
    const formatRemaining = (milliseconds) => {
        const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
        return `${Math.floor(totalSeconds / 60)}:${String(totalSeconds % 60).padStart(2, "0")}`;
    };
    const durationFor = (timer) => {
        const seconds = Number.parseInt(timer.dataset.bakeDurationSeconds || "", 10);
        return (Number.isFinite(seconds) ? seconds * 1000 : DEFAULT_DURATION_MS);
    };

    const effectiveTimerState = (key) => {
        const raw = pieStates[key] || initialPieState(key);
        if (raw.timer_status === "running" && raw.timer_end_at_ms) {
            const remaining = Math.max(Number(raw.timer_end_at_ms) - adjustedNow(), 0);
            return {
                ...raw,
                timer_status: remaining > 0 ? "running" : "done",
                timer_remaining_ms: remaining,
            };
        }
        return raw;
    };

    const renderTimers = () => {
        timersByKey.forEach((timer, key) => {
            const state = effectiveTimerState(key);
            const action = timer.querySelector("[data-bake-timer-action]");
            const display = timer.querySelector("[data-bake-timer-display]");
            const toggle = timer.querySelector("[data-bake-timer-toggle]");
            const status = state.timer_status || "idle";
            timer.dataset.timerStatus = status;
            timer.dataset.timerRemainingMs = String(Math.max(0, Number(state.timer_remaining_ms || 0)));
            timer.dataset.timerEndAtMs = state.timer_end_at_ms ? String(state.timer_end_at_ms) : "";
            timer.classList.toggle("bake-timer--running", status === "running");
            timer.classList.toggle("bake-timer--paused", status === "paused");
            timer.classList.toggle("bake-timer--done", status === "done");
            if (status === "running") {
                action.textContent = "Pause";
                display.textContent = formatRemaining(state.timer_remaining_ms);
                toggle.disabled = false;
            } else if (status === "paused") {
                action.textContent = "Resume";
                display.textContent = formatRemaining(state.timer_remaining_ms);
                toggle.disabled = false;
            } else if (status === "done") {
                action.textContent = "Finished";
                display.textContent = "DONE";
                toggle.disabled = true;
            } else {
                action.textContent = "Start";
                display.textContent = formatRemaining(durationFor(timer));
                toggle.disabled = false;
            }
        });
    };

    const renderOven = () => {
        const occupants = {};
        Object.entries(pieStates).forEach(([key, state]) => {
            if (state.oven_position) {
                occupants[state.oven_position] = key;
            }
        });
        selectorsByKey.forEach((selector, key) => {
            const selectedPosition = pieStates[key]?.oven_position || null;
            selector.dataset.selectedOvenPosition = selectedPosition || "";
            selector.classList.toggle("oven-position-selector--assigned", Boolean(selectedPosition));
            selector.querySelectorAll("[data-oven-position-choice]").forEach((button) => {
                const position = button.dataset.ovenPositionChoice;
                const occupant = occupants[position];
                const selected = occupant === key;
                const occupiedByAnother = Boolean(occupant) && !selected;
                const label = POSITION_LABELS[position] || position;
                button.classList.toggle("is-selected", selected);
                button.classList.toggle("is-occupied", occupiedByAnother);
                button.setAttribute("aria-pressed", selected ? "true" : "false");
                button.title = selected
                    ? `${label} · this pie`
                    : occupiedByAnother
                        ? `${label} · occupied by another pie`
                        : `${label} · available`;
            });
        });
    };

    const formatBoxedAt = (value) => {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return "Ready";
        }
        return date.toLocaleTimeString([], {
            timeZone: serviceTimezone,
            hour: "numeric",
            minute: "2-digit",
            second: "2-digit",
        });
    };
    const renderPizzaCountdown = () => {
        if (!countdown) {
            return;
        }
        const total = Math.max(0, Number.parseInt(board.dataset.totalPizzas || "0", 10) || 0);
        let boxedPizzas = 0;
        rowsByOrderId.forEach((row, orderId) => {
            if (!boxedOrders[orderId]) {
                return;
            }
            boxedPizzas += Math.max(0, Number.parseInt(row.dataset.orderPizzaUnits || "0", 10) || 0);
        });
        const remaining = Math.max(total - boxedPizzas, 0);
        const value = countdown.querySelector("[data-pizza-countdown-value]");
        if (value) {
            value.textContent = String(remaining);
        }
        countdown.classList.toggle("pizza-countdown--finished", remaining === 0 && total > 0);
        countdown.setAttribute("aria-label", `${remaining} pizza${remaining === 1 ? "" : "s"} remaining today`);
    };

    const parseOrderCounts = (row, datasetKey) => {
        try {
            const parsed = JSON.parse(row.dataset[datasetKey] || "{}");
            return parsed && typeof parsed === "object" ? parsed : {};
        } catch (_error) {
            return {};
        }
    };

    const boxedCountsFor = (datasetKey) => {
        const counts = new Map();
        if (!decrementAllDayCounts) {
            return counts;
        }
        rowsByOrderId.forEach((row, orderId) => {
            if (!boxedOrders[orderId]) {
                return;
            }
            Object.entries(parseOrderCounts(row, datasetKey)).forEach(([name, quantity]) => {
                const units = Math.max(0, Number.parseInt(quantity, 10) || 0);
                counts.set(name, (counts.get(name) || 0) + units);
            });
        });
        return counts;
    };

    const renderAllDaySummaryRows = (rows, boxedCounts) => {
        rows.forEach((row) => {
            const fullCount = Math.max(0, Number.parseInt(row.dataset.fullCount || "0", 10) || 0);
            const boxedCount = boxedCounts.get(row.dataset.summaryName || "") || 0;
            const remaining = decrementAllDayCounts ? Math.max(fullCount - boxedCount, 0) : fullCount;
            const count = row.querySelector("[data-summary-count]");
            if (count) {
                const summaryName = row.dataset.summaryName || "";
                count.textContent = `${remaining}× ${summaryName}`;
            }
        });
    };

    const renderAllDayCounts = () => {
        const boxedPies = boxedCountsFor("orderPizzaCounts");
        const boxedModifiers = boxedCountsFor("orderModifierCounts");
        renderAllDaySummaryRows(pieAllDayRows, boxedPies);
        renderAllDaySummaryRows(modifierAllDayRows, boxedModifiers);

        if (pieAllDayTotal) {
            const full = Math.max(0, Number.parseInt(pieAllDayTotal.dataset.fullCount || "0", 10) || 0);
            const boxed = Array.from(boxedPies.values()).reduce((total, value) => total + value, 0);
            const remaining = decrementAllDayCounts ? Math.max(full - boxed, 0) : full;
            pieAllDayTotal.textContent = decrementAllDayCounts ? `${remaining} remaining` : `${remaining} total`;
        }
        if (modifierAllDayTotal) {
            const full = Math.max(0, Number.parseInt(modifierAllDayTotal.dataset.fullCount || "0", 10) || 0);
            const boxed = Array.from(boxedModifiers.values()).reduce((total, value) => total + value, 0);
            const remaining = decrementAllDayCounts ? Math.max(full - boxed, 0) : full;
            modifierAllDayTotal.textContent = decrementAllDayCounts ? `${remaining} portions remaining` : `${remaining} portions`;
        }
    };

    const renderBoxedOrders = () => {
        rowsByOrderId.forEach((row, orderId) => {
            const boxedAt = boxedOrders[orderId] || null;
            const button = row.querySelector("[data-order-boxed-button]");
            const status = row.querySelector("[data-order-ready-status]");
            const time = row.querySelector("[data-order-boxed-time]");
            row.classList.toggle("order-row--boxed", Boolean(boxedAt));
            row.dataset.boxedAt = boxedAt || "";
            if (button) {
                button.classList.toggle("is-boxed", Boolean(boxedAt));
                button.setAttribute("aria-pressed", boxedAt ? "true" : "false");
                button.textContent = boxedAt ? "Undo boxed" : "Mark boxed";
            }
            if (status) {
                status.hidden = !boxedAt;
            }
            if (time) {
                time.dateTime = boxedAt || "";
                time.textContent = boxedAt ? formatBoxedAt(boxedAt) : "";
            }
        });
        renderPizzaCountdown();
        renderAllDayCounts();
    };

    const renderAll = () => {
        renderTimers();
        renderOven();
        renderBoxedOrders();
    };

    const shouldHoldBoardReload = () => (
        Boolean(document.querySelector("dialog[open]"))
        || document.body.classList.contains("walk-in-assignment-pending")
    );

    const applyPayload = (payload) => {
        if (Number.isFinite(Number(payload.server_now_ms))) {
            serverOffsetMs = Number(payload.server_now_ms) - Date.now();
        }
        const remoteRevision = String(payload.board_content_revision || "");
        const localRevision = String(board.dataset.boardContentRevision || "");
        if (remoteRevision && localRevision && remoteRevision !== localRevision) {
            if (!shouldHoldBoardReload()) {
                window.PizzeriaDashboardViewport?.remember();
                window.location.reload();
                return;
            }
        } else if (remoteRevision && !localRevision) {
            board.dataset.boardContentRevision = remoteRevision;
        }
        pieStates = {...pieStates, ...(payload.pies || {})};
        boxedOrders = payload.boxed_orders || {};
        const viewportSnapshot = window.PizzeriaDashboardViewport?.capture();
        renderAll();
        window.PizzeriaDashboardViewport?.restore(viewportSnapshot, {defer: false});
    };

    const postPieUpdate = async (pieKey, changes) => {
        const response = await fetch(updateUrl, {
            method: "POST",
            headers: {"Accept": "application/json", "Content-Type": "application/json"},
            body: JSON.stringify({
                service_date: serviceDate,
                pie_key: pieKey,
                ...changes,
            }),
        });
        const result = await response.json();
        if (!response.ok || !result.ok) {
            throw new Error(result.error || "The shared production state could not be saved.");
        }
        applyPayload(result);
    };

    timersByKey.forEach((timer, key) => {
        timer.querySelector("[data-bake-timer-toggle]")?.addEventListener("click", async (event) => {
            event.preventDefault();
            event.stopPropagation();
            const status = effectiveTimerState(key).timer_status;
            const timerAction = status === "running" ? "pause" : "start";
            timer.classList.add("is-saving");
            try {
                await postPieUpdate(key, {timer_action: timerAction, duration_ms: durationFor(timer)});
            } catch (error) {
                window.alert(String(error));
            } finally {
                timer.classList.remove("is-saving");
            }
        });
        timer.querySelector("[data-bake-timer-reset]")?.addEventListener("click", async (event) => {
            event.preventDefault();
            event.stopPropagation();
            timer.classList.add("is-saving");
            try {
                await postPieUpdate(key, {timer_action: "reset", duration_ms: durationFor(timer)});
            } catch (error) {
                window.alert(String(error));
            } finally {
                timer.classList.remove("is-saving");
            }
        });
    });

    selectorsByKey.forEach((selector, key) => {
        selector.addEventListener("dragstart", (event) => event.preventDefault());
        selector.querySelectorAll("[data-oven-position-choice]").forEach((button) => {
            button.addEventListener("click", async (event) => {
                event.preventDefault();
                event.stopPropagation();
                const requested = button.dataset.ovenPositionChoice;
                const ovenPosition = pieStates[key]?.oven_position === requested ? null : requested;
                selector.classList.add("is-saving");
                try {
                    await postPieUpdate(key, {oven_position: ovenPosition});
                } catch (error) {
                    window.alert(String(error));
                } finally {
                    selector.classList.remove("is-saving");
                }
            });
        });
    });

    orderRows.forEach((row) => {
        row.querySelector("[data-order-boxed-button]")?.addEventListener("click", async (event) => {
            event.preventDefault();
            event.stopPropagation();
            const button = event.currentTarget;
            const orderId = row.dataset.orderId;
            const boxed = !Boolean(boxedOrders[orderId]);
            button.disabled = true;
            try {
                const response = await fetch(orderReadyUrl, {
                    method: "POST",
                    headers: {"Accept": "application/json", "Content-Type": "application/json"},
                    body: JSON.stringify({service_date: serviceDate, order_id: orderId, boxed}),
                });
                const result = await response.json();
                if (!response.ok || !result.ok) {
                    throw new Error(result.error || "The boxed status could not be saved.");
                }
                if (result.boxed_at) {
                    boxedOrders[orderId] = result.boxed_at;
                } else {
                    delete boxedOrders[orderId];
                }
                renderBoxedOrders();
            } catch (error) {
                window.alert(String(error));
            } finally {
                button.disabled = false;
            }
        });
    });

    const poll = async () => {
        if (polling || document.hidden) {
            return;
        }
        polling = true;
        try {
            const response = await fetch(`${stateUrl}?date=${encodeURIComponent(serviceDate)}`, {
                headers: {"Accept": "application/json"},
                cache: "no-store",
            });
            const result = await response.json();
            if (response.ok && result.ok) {
                applyPayload(result);
            }
        } catch (_error) {
            // Keep the last known production state during a brief network interruption.
        } finally {
            polling = false;
        }
    };

    renderAll();
    poll();
    window.setInterval(renderTimers, 250);
    window.setInterval(poll, 2_000);
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
            poll();
        }
    });
})();

(() => {
    const board = document.getElementById("production-board");
    if (!board) {
        return;
    }

    const incrementalSyncAvailable = board.dataset.incrementalSyncAvailable === "true";
    const autoSyncAvailable = board.dataset.autoSyncAvailable === "true";
    if (!incrementalSyncAvailable && !autoSyncAvailable) {
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

    let enabled = autoSyncAvailable && board.dataset.autoSyncEnabled === "true";
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
        incrementalButton.disabled = busy || !incrementalSyncAvailable;
        incrementalButton.textContent = busy ? "Checking Square…" : "Incremental update";
    };

    const schedule = (delaySeconds = seconds) => {
        window.clearTimeout(timerId);
        timerId = null;
        if (!autoSyncAvailable) {
            return;
        }
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
        if (!autoSyncAvailable || !settingsUrl) {
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
                body: JSON.stringify({
                    service_date: serviceDate,
                    board_content_revision: board.dataset.boardContentRevision || "",
                }),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) {
                throw new Error(result.error || "Square refresh failed.");
            }

            const orderChanges = Number(result.changed_count || 0) + Number(result.removed_count || 0);
            const historyChanges = Number(result.customer_history_changed || 0);
            const changes = orderChanges + historyChanges;
            const localContentChanged = Boolean(result.board_content_changed);
            board.dataset.boardContentRevision = result.board_content_revision || "";
            if (changes > 0 || localContentChanged) {
                const changeLabel = changes > 0
                    ? `${changes} dashboard change${changes === 1 ? "" : "s"}`
                    : "Local order changes";
                setStatus(`${changeLabel} found—updating…`);
                window.PizzeriaDashboardViewport?.remember();
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
            if (autoSyncAvailable) {
                schedule();
            }
        }
    };

    if (incrementalSyncAvailable) {
        incrementalButton?.addEventListener("click", () => runQuickSync(true));
    }

    if (autoSyncAvailable) toggle?.addEventListener("change", () => {
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

    if (autoSyncAvailable) intervalSelect?.addEventListener("change", () => {
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

    if (autoSyncAvailable) {
        if (enabled) {
            schedule(3);
        } else {
            setStatus("Auto refresh stopped");
        }
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
