(() => {
    const board = document.getElementById("production-board");
    if (!board?.dataset.pickupEventUrl) return;
    const controls = new Map();
    for (const row of board.querySelectorAll(".order-row[data-order-id]")) {
        const buttons = Array.from(row.querySelectorAll("button[data-pickup-event]"));
        if (buttons.length) controls.set(row.dataset.orderId, buttons);
    }
    if (!controls.size) return;

    const saving = new Set();
    let lastSave = -Infinity;
    function render(buttons, marks) {
        for (const button of buttons) {
            const kind = button.dataset.pickupEvent;
            const mark = marks[kind];
            const token = mark?.recorded_at || "";
            const blocked = !mark && kind === "arrived" && Boolean(marks.picked_up);
            if (button.dataset.recordedAt === token && button.disabled === blocked) continue;
            button.dataset.recordedAt = token;
            button.setAttribute("aria-pressed", String(Boolean(mark)));
            button.disabled = blocked;
            button.title = mark ? [mark.schedule, "Tap to undo this timestamp."].filter(Boolean).join(". ") : "";
            button.querySelector("[data-pickup-label]").textContent = mark ? button.dataset.doneLabel
                : blocked ? button.dataset.missingLabel : button.dataset.actionLabel;
            const clock = button.querySelector("[data-pickup-clock]");
            clock.textContent = mark?.clock || "";
            if (token) clock.dateTime = token;
            else clock.removeAttribute("datetime");
            button.querySelector("[data-pickup-offset]").textContent = mark?.offset || "";
        }
    }

    for (const [orderId, buttons] of controls) {
        for (const button of buttons) {
            button.addEventListener("click", async (event) => {
                event.preventDefault();
                event.stopPropagation();
                if (saving.has(orderId) || button.disabled) return;
                const recorded = button.dataset.recordedAt;
                if (recorded && !window.confirm(`Undo “${button.dataset.doneLabel}” for this order?`)) return;
                const disabledBefore = buttons.map((b) => b.disabled);
                saving.add(orderId);
                buttons.forEach((b) => { b.disabled = true; });
                let saved = null;
                try {
                    const response = await fetch(board.dataset.pickupEventUrl, {
                        method: "POST",
                        headers: {"Accept": "application/json", "Content-Type": "application/json"},
                        body: JSON.stringify({
                            service_date: board.dataset.serviceDate, order_id: orderId, event: button.dataset.pickupEvent,
                            ...(recorded ? {undo_recorded_at: recorded} : {}),
                        }),
                    });
                    const result = await response.json();
                    if (!response.ok || !result.ok) throw new Error(result.error || "Timestamp was not saved.");
                    saved = result.marks;
                    lastSave = performance.now();
                } catch (error) {
                    window.alert(String(error));
                } finally {
                    saving.delete(orderId);
                    buttons.forEach((b, index) => { b.disabled = disabledBefore[index]; });
                    if (saved) {
                        const paint = () => render(buttons, saved);
                        if (window.PizzeriaDashboardViewport) window.PizzeriaDashboardViewport.preserve(paint);
                        else paint();
                    }
                }
            });
        }
    }

    // Reuse the board's existing two-second poll and scroll preservation. A
    // stale response must not overwrite a local tap or re-enable its button.
    document.addEventListener("pizzeria-dashboard:pickup-marks", ({detail}) => {
        if (detail.startedAt <= lastSave) return;
        for (const [orderId, buttons] of controls) {
            if (!saving.has(orderId)) render(buttons, detail.marks[orderId] || {});
        }
    });
})();
