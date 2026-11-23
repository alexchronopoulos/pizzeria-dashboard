// Run with: node --test tests/js/pickup-tracking.test.cjs
const test = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {resolve} = require("node:path");
const vm = require("node:vm");
const source = readFileSync(resolve(__dirname, "../../pizzeria_dashboard/static/pickup-tracking.js"), "utf8");

function button(kind, action, done, missing) {
    const parts = Object.fromEntries(["label", "clock", "offset"].map(part => [
        `[data-pickup-${part}]`, {textContent: part === "label" ? action : "", removeAttribute() { delete this.dateTime; }},
    ]));
    return Object.assign(new EventTarget(), {
        parts, disabled: false, querySelector: selector => parts[selector],
        setAttribute(name, value) { this[name] = value; },
        dataset: {pickupEvent: kind, recordedAt: "", actionLabel: action, doneLabel: done, missingLabel: missing},
    });
}

function setup() {
    const arrived = button("arrived", "Mark Arrived", "Arrived", "Arrival not recorded");
    const pickup = button("picked_up", "Mark Picked Up", "Picked up", "Pickup not recorded");
    const row = {dataset: {orderId: "A"}, querySelectorAll: () => [arrived, pickup]};
    const board = {dataset: {serviceDate: "2026-08-06", pickupEventUrl: "/order-pickup-event"}, querySelectorAll: () => [row]};
    const document = Object.assign(new EventTarget(), {getElementById: () => board});
    const requests = [], errors = [];
    const window = {alert: e => errors.push(e), confirm: () => true, PizzeriaDashboardViewport: {preserve: fn => fn()}};
    const fetch = (url, params) => new Promise(resolve => {
        requests.push({url, body: JSON.parse(params.body), reply: result => resolve({ok: result.ok, json: async () => result})});
    });
    vm.runInNewContext(source, {document, window, fetch, performance: {now: () => 20}});
    const poll = (startedAt, marks) => document.dispatchEvent(Object.assign(new Event("pizzeria-dashboard:pickup-marks"), {
        detail: {startedAt, marks: {A: marks}},
    }));
    const click = b => b.dispatchEvent(new Event("click", {cancelable: true}));
    return {arrived, pickup, requests, errors, window, poll, click};
}

const mark = {recorded_at: "2026-08-06T20:52:00+00:00", clock: "4:52 PM", status: "early",
    offset: "8 min early", schedule: "Scheduled for 5:00 PM"};
const flush = () => new Promise(r => setImmediate(r));

test("one tap posts once; late polls cannot erase it; undo uses the observed timestamp", async () => {
    const {arrived, pickup, requests, errors, window, poll, click} = setup();
    click(arrived); click(arrived); click(pickup);
    assert.equal(requests.length, 1);
    assert.deepEqual(requests[0].body, {event: "arrived", service_date: "2026-08-06", order_id: "A"});
    assert.equal(requests[0].url, "/order-pickup-event");
    poll(10, {}); assert.equal(arrived.disabled, true);
    requests[0].reply({ok: true, marks: {arrived: mark}}); await flush();
    poll(15, {});
    assert.equal(arrived.dataset.recordedAt, mark.recorded_at);
    assert.equal(arrived["aria-pressed"], "true");
    assert.equal(arrived.parts["[data-pickup-offset]"].textContent, "8 min early");
    assert.equal(arrived.parts["[data-pickup-clock]"].textContent, "4:52 PM");
    window.confirm = () => false; click(arrived); assert.equal(requests.length, 1);
    window.confirm = () => true; click(arrived);
    assert.equal(requests[1].body.undo_recorded_at, mark.recorded_at);
    requests[1].reply({ok: false, error: "Timestamp changed."}); await flush();
    assert.equal(errors.length, 1); assert.equal(arrived.disabled, false);
    assert.equal(arrived.dataset.recordedAt, mark.recorded_at);
    click(arrived); requests[2].reply({ok: true, marks: {}}); await flush();
    assert.equal(arrived.parts["[data-pickup-label]"].textContent, "Mark Arrived");
    assert.equal(arrived["aria-pressed"], "false");
});

test("a pickup from another display does not create an arrival", () => {
    const {arrived, pickup, requests, poll, click} = setup();
    poll(1, {picked_up: mark});
    assert.equal(pickup.dataset.recordedAt, mark.recorded_at);
    assert.equal(arrived.disabled, true);
    assert.equal(arrived.parts["[data-pickup-label]"].textContent, "Arrival not recorded");
    click(arrived); assert.equal(requests.length, 0);
    poll(2, {}); assert.equal(arrived.disabled, false);
    assert.equal(pickup.parts["[data-pickup-clock]"].textContent, "");
});
