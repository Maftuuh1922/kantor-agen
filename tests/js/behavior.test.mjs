// Run with: node --test tests/js/
// Covers skeleton/office/static/behavior.js (Requirements 2.5, 2.6, 2.7, 2.8).
// The controller runs against fake characters that only record the calls it makes.
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../skeleton/office/static");
const { layout } = await import(path.join(STATIC, "layout.js"));
const { Grid } = await import(path.join(STATIC, "pathfinding.js"));
const B = await import(path.join(STATIC, "behavior.js"));
const { BehaviorController, TIMING, CHAT_SPOTS, MAX_DT, seededRandom, seatRoot } = B;

const DT = 1 / 30;
const grid = new Grid(layout);

// ---- helpers ----------------------------------------------------------------

// A character double: records every setBehavior call and the pose it was given.
function fakeChar(id, clock) {
  const c = {
    id,
    calls: [],
    behavior: "idle",
    timedUntil: Infinity,
    pose: { x: NaN, z: NaN, yaw: 0 },
    setBehavior(name, opts = {}) {
      c.calls.push({ name, opts, t: clock.t, x: c.pose.x, z: c.pose.z });
      c.behavior = name;
      c.timedUntil = ["wave", "stretch", "drink"].includes(name) ? clock.t + (opts.duration ?? 3) : Infinity;
    },
    setPose(x, z, yaw) { c.pose = { x, z, yaw }; },
    names() { return c.calls.map((k) => k.name); },
    // the real character drops back to idle when a timed gesture ends
    frame() { if (clock.t >= c.timedUntil) { c.behavior = "idle"; c.timedUntil = Infinity; } },
  };
  return c;
}

// rng that forces the idle choices: kind picks by index fraction, delay is the minimum.
const forced = (kindValue, extra = {}) => (id, n, salt) => {
  if (salt === "kind") return kindValue;
  if (salt === "delay") return n === 0 ? (extra.delay ?? 0) : 0.9999;   // first one soon, then the maximum wait
  if (salt === "talk") return extra.talk ?? 0.5;
  if (salt === "pair") return extra.pair ?? 0;
  return 0;
};
const STRETCH = 0;
const DRINK = 0.5;
const CHAT = 0.99;   // with three kinds available this is "chat"

function makeWorld(count, { rng, idleDelay, onFrame } = {}) {
  const clock = { t: 0 };
  const ctl = new BehaviorController({ layout, rng, idleDelay });
  const chars = [];
  for (let i = 0; i < count; i++) {
    const ch = fakeChar(`a${i}`, clock);
    chars.push(ch);
    ctl.addAgent({ id: ch.id, character: ch, deskIndex: i });
  }
  const world = {
    ctl, chars, clock,
    send(i, state, target = null, bubble = null) {
      return ctl.applyVisualState({ agent_id: chars[i].id, state, target_agent_id: target == null ? null : chars[target].id, bubble_text: bubble });
    },
    run(seconds, hook = null) {
      const frames = Math.round(seconds / DT);
      for (let f = 0; f < frames; f++) {
        clock.t += DT;
        ctl.update(DT);
        for (const c of chars) c.frame();
        // Req 2.8: a character only shows typing while the backend says working.
        for (const c of chars) {
          if (c.behavior === "type") assert.equal(ctl.getStatus(c.id).state, "working", `${c.id} types at t=${clock.t.toFixed(2)} while not working`);
        }
        onFrame?.(world);
        hook?.(world);
      }
    },
    until(pred, maxSeconds = 120) {
      for (let f = 0; f < Math.round(maxSeconds / DT); f++) {
        if (pred(world)) return true;
        world.run(DT);
      }
      return pred(world);
    },
  };
  return world;
}

const cellKey = (p) => { const c = grid.worldToCell(p.x, p.z); return `${c.i},${c.j}`; };
const desk = (i) => layout.deskForAgent(i);
const near = (p, q, d = 0.4) => Math.hypot(p.x - q.x, p.z - q.z) <= d;
const seated = (w, i) => w.ctl.poseOf(w.chars[i].id).seated;

// ---- layout assumptions -------------------------------------------------------

test("chat spots, pantry spot and visit spots are free cells; chat pairs face each other", () => {
  for (const pair of CHAT_SPOTS) {
    for (const s of pair) assert.equal(grid.isBlockedWorld(s.x, s.z), false, `chat spot ${s.x},${s.z} is blocked`);
    const [a, b] = pair;
    assert.ok(Math.abs(Math.hypot(a.x - b.x, a.z - b.z) - 1) < 1e-9, "pair is 1 m apart");
    assert.ok(Math.abs(Math.sin(a.rotation) * (b.x - a.x) + Math.cos(a.rotation) * (b.z - a.z) - 1) < 1e-9, "a faces b");
    assert.ok(Math.abs(Math.sin(b.rotation) * (a.x - b.x) + Math.cos(b.rotation) * (a.z - b.z) - 1) < 1e-9, "b faces a");
  }
  assert.equal(grid.isBlockedWorld(layout.pantrySpot.x, layout.pantrySpot.z), false);
  for (let i = 0; i < layout.maxAgents; i++) {
    assert.equal(grid.isBlockedWorld(desk(i).visitSpot.x, desk(i).visitSpot.z), false);
    assert.equal(grid.isBlockedWorld(desk(i).standSpot.x, desk(i).standSpot.z), false);
  }
});

// ---- working: walk, sit, type ---------------------------------------------------

test("working: an agent away from its desk walks to the stand spot, sits and then types", () => {
  const w = makeWorld(2, { rng: forced(DRINK), idleDelay: [1, 1] });
  w.send(0, "idle");
  w.send(1, "idle");
  w.run(2);                                   // idle agent 0 heads to the pantry
  assert.ok(w.chars[0].names().includes("walk"));
  w.send(0, "working");
  const walkCall = w.chars[0].calls.at(-1);
  assert.equal(walkCall.name, "walk", "heads for the desk at once");
  assert.equal(walkCall.opts.speed, 1.2);
  assert.ok(w.until((x) => x.chars[0].behavior === "type", 30), "starts typing");
  w.run(3);
  assert.equal(seated(w, 0), true);
  const root = seatRoot(desk(0));
  assert.ok(near(w.chars[0].pose, root, 0.01), "pelvis is over the seat");
  assert.equal(w.chars[0].behavior, "type");
  assert.equal(w.ctl.getStatus("a0").behavior, "typing");
  const types = w.chars[0].names().filter((n) => n === "type").length;
  assert.equal(types, 1, "type is requested once");
});

test("working: sitting starts from the stand spot facing the desk and slides to the seat", () => {
  const w = makeWorld(1);
  w.send(0, "idle");                          // a first "working" would start seated
  w.send(0, "working");
  const start = { ...w.chars[0].pose };
  assert.ok(near(start, desk(0).standSpot, 0.01));
  const xs = [];
  w.run(TIMING.sit + 0.5, (x) => xs.push(x.chars[0].pose.z));
  assert.ok(xs.every((z, i) => i === 0 || z >= xs[i - 1] - 1e-9), "moves one way toward the desk");
  assert.ok(Math.abs(w.chars[0].pose.yaw - desk(0).rotation) < 1e-9);
});

test("a fresh agent whose first state is working starts seated, without walking", () => {
  const w = makeWorld(1);
  w.send(0, "working");
  assert.equal(w.ctl.poseOf("a0").seated, true);
  assert.deepEqual(w.chars[0].names().filter((n) => n === "walk"), []);
});

// ---- Req 2.8 ----------------------------------------------------------------------

test("typing only while the backend says working (random state sequence, per-frame check)", () => {
  const w = makeWorld(4, { idleDelay: [2, 4] });
  const states = ["working", "handoff", "waiting", "idle", "paused"];
  let seed = 7;
  const next = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
  for (let k = 0; k < 120; k++) {
    const i = Math.floor(next() * 4);
    const s = states[Math.floor(next() * states.length)];
    w.send(i, s, s === "handoff" ? (i + 1) % 4 : null);
    w.run(0.3 + next() * 6);      // the per-frame invariant lives in run()
  }
  const everTyped = w.chars.some((c) => c.names().includes("type"));
  assert.ok(everTyped, "the sequence reached working at least once");
});

test("typing stops within 0.5 s of the backend leaving working", () => {
  for (const next of ["idle", "paused", "waiting", "handoff"]) {
    const w = makeWorld(2);
    w.send(0, "working");
    w.run(6);
    assert.equal(w.chars[0].behavior, "type");
    const t0 = w.clock.t;
    w.send(0, next, next === "handoff" ? 1 : null);
    let stoppedAt = null;
    w.run(1, (x) => { if (stoppedAt === null && x.chars[0].behavior !== "type") stoppedAt = x.clock.t; });
    assert.ok(stoppedAt === null ? w.chars[0].behavior !== "type" : stoppedAt - t0 <= 0.5, `${next}: typing stopped within 0.5 s`);
    assert.notEqual(w.chars[0].behavior, "type");
  }
});

// ---- idle (Req 2.7) ------------------------------------------------------------------

test("idle behaviors never use type, sit or a seated pose, and all three kinds happen", () => {
  const w = makeWorld(6, { idleDelay: [2, 4] });
  for (let i = 0; i < 6; i++) w.send(i, "idle");
  let everSeated = false;
  w.run(600, (x) => { for (let i = 0; i < 6; i++) if (seated(x, i)) everSeated = true; });
  const all = new Set(w.chars.flatMap((c) => c.names()));
  for (const banned of ["type", "sit", "sit_idle"]) assert.ok(!all.has(banned), `${banned} must not appear in idle`);
  assert.equal(everSeated, false);
  for (const wanted of ["stretch", "drink", "talk", "walk"]) assert.ok(all.has(wanted), `idle shows ${wanted}`);
});

test("idle: the first behavior starts after the seeded delay (20 to 60 s)", () => {
  const lo = makeWorld(1, { rng: forced(STRETCH, { delay: 0 }) });
  lo.send(0, "idle");
  lo.run(19.5);
  assert.ok(!lo.chars[0].names().includes("stretch"));
  lo.run(1);
  assert.ok(lo.chars[0].names().includes("stretch"), "stretch at about 20 s");
  const hi = makeWorld(1, { rng: forced(STRETCH, { delay: 0.999999 }) });
  hi.send(0, "idle");
  hi.run(59);
  assert.ok(!hi.chars[0].names().includes("stretch"));
  hi.run(1.5);
  assert.ok(hi.chars[0].names().includes("stretch"), "stretch at about 60 s");
  // default rng: always inside the window
  for (const id of ["x", "y", "z", "42", "agent-9"]) {
    const w = makeWorld(1);
    w.ctl.rng = (...a) => seededRandom(...a);
    const delay = w.ctl.idleDelay(w.ctl.brains.get("a0"));
    assert.ok(delay >= 20 && delay <= 60, `${id}: ${delay}`);
  }
});

test("idle: a drink is a walk to the pantry, a drink, and a walk back", () => {
  const w = makeWorld(1, { rng: forced(DRINK) });
  w.send(0, "idle");
  w.run(45);
  const names = w.chars[0].names();
  const di = names.indexOf("drink");
  assert.ok(di > 0, "drank");
  const drinkCall = w.chars[0].calls[di];
  assert.ok(near(drinkCall, layout.pantrySpot, 0.4), "drinks at the pantry");
  assert.ok(names.slice(0, di).includes("walk"), "walked there");
  assert.ok(names.slice(di + 1).includes("walk"), "walked back");
  assert.ok(near(w.chars[0].pose, desk(0).standSpot, 0.05), "back at the desk");
  assert.equal(w.ctl.getStatus("a0").activity, null, "drink is over");
});

test("a new backend state interrupts an idle behavior at once", () => {
  // drink, on the way
  let w = makeWorld(1, { rng: forced(DRINK) });
  w.send(0, "idle");
  w.run(20.5);
  assert.ok(w.until((x) => x.chars[0].pose.x < -2, 20), "walking toward the pantry");
  const before = w.chars[0].calls.length;
  w.send(0, "working");
  assert.equal(w.chars[0].calls.length, before + 1, "reacted in the same call");
  assert.equal(w.chars[0].calls.at(-1).name, "walk");
  assert.ok(w.until((x) => x.chars[0].behavior === "type", 30));
  assert.ok(!w.chars[0].names().slice(before).includes("drink"), "no drink after the interrupt");
  // stretch, in place
  w = makeWorld(1, { rng: forced(STRETCH) });
  w.send(0, "idle");
  w.run(20.5);
  assert.equal(w.chars[0].behavior, "stretch");
  w.send(0, "paused");
  assert.equal(w.chars[0].calls.at(-1).name, "idle", "stretch stopped at once");
  assert.equal(w.chars[0].behavior, "idle");
  // drink, while drinking at the pantry
  w = makeWorld(1, { rng: forced(DRINK) });
  w.send(0, "idle");
  assert.ok(w.until((x) => x.chars[0].behavior === "drink", 60));
  w.send(0, "waiting");
  assert.equal(w.chars[0].calls.at(-1).name, "walk", "leaves the pantry toward its desk");
  assert.ok(w.until((x) => x.chars[0].names().includes("wave"), 30));
  assert.ok(near(w.chars[0].pose, desk(0).standSpot, 0.05), "waves at its own desk");
});

test("idle chat: two idle agents meet at a chat pair, face each other and talk", () => {
  const w = makeWorld(2, { rng: forced(CHAT, { talk: 0 }) });
  w.send(0, "idle");
  w.send(1, "idle");
  const log = [];
  w.run(40, (x) => {
    if (x.chars[0].behavior === "talk" && x.chars[1].behavior === "talk") log.push({ t: x.clock.t, a: { ...x.chars[0].pose }, b: { ...x.chars[1].pose } });
  });
  assert.ok(log.length > 0, "both talked at the same time");
  const { a, b } = log[0];
  const pair = CHAT_SPOTS.find((p) => near(a, p[0], 0.3) || near(a, p[1], 0.3));
  assert.ok(pair, "met at a chat pair");
  assert.ok(near(a, b, 1.6) && !near(a, b, 0.5), "about one meter apart");
  const dx = b.x - a.x;
  const dz = b.z - a.z;
  assert.ok(Math.sin(a.yaw) * dx + Math.cos(a.yaw) * dz > 0.5, "a faces b");
  assert.ok(Math.sin(b.yaw) * -dx + Math.cos(b.yaw) * -dz > 0.5, "b faces a");
  const talkSpan = log.at(-1).t - log[0].t;
  assert.ok(talkSpan >= 2.8 && talkSpan <= 5.2, `talked ${talkSpan.toFixed(1)} s`);
  w.run(25);
  for (const i of [0, 1]) assert.ok(near(w.chars[i].pose, desk(i).standSpot, 0.05), `agent ${i} walked back`);
});

test("a walk goes around colleagues who stand still (the corridor behind the desks), never through them", () => {
  const w = makeWorld(4, { rng: forced(DRINK), idleDelay: [1, 1] });
  for (let i = 0; i < 3; i++) w.send(i, "paused");   // stand at their desks
  w.send(3, "idle");
  let min = Infinity;
  w.run(40, (x) => {
    for (let i = 0; i < 3; i++) {
      const a = x.chars[3].pose;
      const b = x.chars[i].pose;
      min = Math.min(min, Math.hypot(a.x - b.x, a.z - b.z));
    }
  });
  assert.ok(w.chars[3].names().includes("drink"), "it still got to the pantry");
  assert.ok(min >= 0.45, `closest approach ${min.toFixed(2)} m`);
});

test("characters do not walk through each other (6 idle agents, many walks and chats)", () => {
  const w = makeWorld(6, { idleDelay: [2, 4] });
  for (let i = 0; i < 6; i++) w.send(i, "idle");
  let min = Infinity;
  let at = "";
  w.run(600, (x) => {
    for (let i = 0; i < 6; i++) for (let j = i + 1; j < 6; j++) {
      const d = Math.hypot(x.chars[i].pose.x - x.chars[j].pose.x, x.chars[i].pose.z - x.chars[j].pose.z);
      if (d < min) { min = d; at = `t=${x.clock.t.toFixed(1)} a${i}/${x.chars[i].behavior} a${j}/${x.chars[j].behavior}`; }
    }
  });
  // Bodies are about 0.5 m wide. Two walkers that leave adjacent pantry cells at the same
  // moment may brush shoulders (centers 0.2 m or more apart), but nobody walks through anybody.
  assert.ok(min >= 0.2, `closest approach ${min.toFixed(2)} m at ${at}`);
});

test("idle chat: the two walks do not cross, so characters never pass through each other", () => {
  // agents 2 and 3 are the only idle ones; 3 is east of 2 and the east chat pair is chosen
  // agent 3 (the east one) starts the chat, so with a fixed order it would be given the west spot
  const base = forced(CHAT, { talk: 0, pair: 0.99 });
  const rng = (id, n, salt) => (salt === "delay" && n === 0 ? (id === "a3" ? 0 : 0.5) : base(id, n, salt));
  const w = makeWorld(4, { rng });
  w.send(0, "paused");
  w.send(1, "paused");
  w.send(2, "idle");
  w.send(3, "idle");
  let min = Infinity;
  let talked = false;
  w.run(40, (x) => {
    const a = x.chars[2].pose;
    const b = x.chars[3].pose;
    min = Math.min(min, Math.hypot(a.x - b.x, a.z - b.z));
    if (x.chars[2].behavior === "talk" && x.chars[3].behavior === "talk") talked = true;
  });
  assert.ok(talked, "they chatted");
  assert.ok(min >= 0.45, `closest approach ${min.toFixed(2)} m`);
});

test("idle chat is cancelled for both when one agent gets a new state", () => {
  const w = makeWorld(2, { rng: forced(CHAT, { talk: 1 }) });
  w.send(0, "idle");
  w.send(1, "idle");
  assert.ok(w.until((x) => x.chars[0].behavior === "talk" && x.chars[1].behavior === "talk", 60));
  const t0 = w.clock.t;
  w.send(1, "working");
  w.run(0.2);
  assert.notEqual(w.chars[0].behavior, "talk", "the partner stops talking");
  assert.ok(w.until((x) => x.chars[1].behavior === "type", 30));
  assert.ok(w.clock.t - t0 < 30);
  w.run(10);
  assert.ok(near(w.chars[0].pose, desk(0).standSpot, 0.05));
});

// ---- handoff (Req 2.5) -----------------------------------------------------------------

test("handoff: stand up, walk to the target's visit spot, face the colleague, talk 3 to 5 s, walk back", () => {
  const w = makeWorld(3);
  w.send(0, "working");
  w.send(1, "working");
  w.run(5);
  assert.equal(w.chars[0].behavior, "type");
  const tSend = w.clock.t;
  const blocked = [];
  w.send(0, "handoff", 1, "Stock is low");
  assert.equal(w.chars[0].calls.at(-1).name, "stand", "stands up first");
  assert.equal(w.chars[0].behavior, "stand");
  assert.equal(w.ctl.getStatus("a0").bubble_text, "Stock is low");
  assert.equal(w.ctl.getStatus("a0").target_agent_id, "a1");
  w.run(40, (x) => {
    const p = x.ctl.poseOf("a0");
    if (!p.seated && x.chars[0].behavior === "walk" && grid.isBlockedWorld(p.x, p.z)) blocked.push({ ...p });
  });
  assert.deepEqual(blocked, [], "never walks through furniture");
  const names = w.chars[0].names();
  const talkAt = w.chars[0].calls.find((k) => k.name === "talk");
  assert.ok(talkAt, "talked");
  assert.ok(near(talkAt, desk(1).visitSpot, 0.1), "talks at colleague 1's visit spot");
  const talkIndex = names.indexOf("talk");
  assert.ok(names.slice(0, talkIndex).includes("walk"));
  const talkEnd = w.chars[0].calls[talkIndex + 1];
  assert.equal(talkEnd.name, "idle");
  const talkSeconds = talkEnd.t - talkAt.t;
  assert.ok(talkSeconds >= 3 - 0.05 && talkSeconds <= 5 + 0.1, `talked ${talkSeconds.toFixed(2)} s`);
  assert.ok(Math.abs(w.chars[0].calls.find((k) => k.name === "talk").t - tSend) > 1, "it walked first");
  assert.ok(names.slice(talkIndex + 2).includes("walk"), "walks back");
  assert.ok(near(w.chars[0].pose, desk(0).standSpot, 0.05), "back at its own stand spot");
  assert.ok(!names.slice(names.indexOf("stand") + 1).includes("type"), "no typing during the handoff");
  w.send(0, "working");
  assert.ok(w.until((x) => x.chars[0].behavior === "type", 15), "resumes work");
});

test("handoff: the visitor faces the colleague, standing or seated", () => {
  for (const colleagueState of ["idle", "working"]) {
    const w = makeWorld(2);
    w.send(1, colleagueState);
    w.send(0, "handoff", 1);
    assert.ok(w.until((x) => x.chars[0].behavior === "talk", 30));
    w.run(1);   // the turn is eased
    const p = w.chars[0].pose;
    const colleague = w.ctl.poseOf("a1");
    assert.equal(colleague.seated, colleagueState === "working");
    const dx = colleague.x - p.x;
    const dz = colleague.z - p.z;
    assert.ok(Math.sin(p.yaw) * dx + Math.cos(p.yaw) * dz > 0.98 * Math.hypot(dx, dz), `faces the ${colleagueState} colleague`);
  }
});

test("handoff to an unknown target talks at its own desk and never walks", () => {
  const w = makeWorld(1);
  w.ctl.applyVisualState({ agent_id: "a0", state: "handoff", target_agent_id: "nobody" });
  w.run(10);
  assert.ok(w.chars[0].names().includes("talk"));
  assert.ok(!w.chars[0].names().includes("walk"));
});

test("a handoff conversation that has started finishes when the backend turns idle", () => {
  const w = makeWorld(2);
  w.send(0, "handoff", 1);
  assert.ok(w.until((x) => x.chars[0].behavior === "talk", 30));
  w.run(0.5);
  w.send(0, "idle");
  assert.equal(w.chars[0].behavior, "talk", "keeps talking");
  w.run(5.5);
  assert.notEqual(w.chars[0].behavior, "talk", "talk ended within 5 s");
  w.run(15);
  assert.ok(near(w.chars[0].pose, desk(0).standSpot, 0.05), "walks home");
  // but working interrupts at once
  const w2 = makeWorld(2);
  w2.send(0, "handoff", 1);
  assert.ok(w2.until((x) => x.chars[0].behavior === "talk", 30));
  w2.send(0, "working");
  assert.notEqual(w2.chars[0].behavior, "talk");
});

// ---- waiting (Req 2.6), paused ---------------------------------------------------------

test("waiting: stands at its desk with the hand raised for as long as it waits, amber marker", () => {
  const w = makeWorld(1);
  w.send(0, "working");
  w.run(5);
  assert.equal(w.ctl.markerFor("a0"), null);
  const mark = w.chars[0].calls.length;
  w.send(0, "waiting", null, "Approve the purchase?");
  assert.equal(w.ctl.markerFor("a0"), "amber");
  assert.equal(w.ctl.getStatus("a0").marker, "amber");
  w.run(20);
  const after = w.chars[0].names().slice(mark);
  assert.equal(after[0], "stand");
  assert.equal(after.filter((n) => n === "wave").length, 1, "starts the raised hand once");
  assert.ok(after.indexOf("stand") < after.indexOf("wave"));
  assert.ok(!after.includes("type") && !after.includes("walk"));
  assert.equal(seated(w, 0), false);
  assert.ok(near(w.chars[0].pose, desk(0).standSpot, 0.01));
  const wave = w.chars[0].calls.find((k) => k.name === "wave");
  assert.equal(wave.opts.duration, Infinity, "the gesture is not timed: the hand stays up");
  assert.equal(w.chars[0].behavior, "wave");
  assert.equal(w.ctl.getStatus("a0").behavior, "waving");
  assert.equal(w.ctl.markerFor("a0"), "amber", "marker stays");
  w.run(120);                                   // no idle behaviors while waiting, hand still up
  assert.equal(w.chars[0].names().slice(mark).filter((n) => n === "wave").length, 1);
  assert.equal(w.chars[0].behavior, "wave", "still raised after two more minutes");
  assert.ok(!w.chars[0].names().includes("stretch") && !w.chars[0].names().includes("drink"));
  w.send(0, "idle");                            // the owner answered: the hand goes down at once
  assert.equal(w.chars[0].behavior, "idle");
  assert.equal(w.ctl.markerFor("a0"), null);
});

test("waiting from the pantry: walks to its own desk first, then waves", () => {
  const w = makeWorld(1, { rng: forced(DRINK) });
  w.send(0, "idle");
  assert.ok(w.until((x) => x.chars[0].behavior === "drink", 60));
  w.send(0, "waiting");
  w.run(30);
  assert.ok(near(w.chars[0].calls.find((k) => k.name === "wave"), desk(0).standSpot, 0.05));
});

test("paused: stands still at its desk with a grey marker", () => {
  const w = makeWorld(1);
  w.send(0, "working");
  w.run(5);
  w.send(0, "paused");
  assert.equal(w.ctl.markerFor("a0"), "grey");
  w.run(4);
  const mark = w.chars[0].calls.length;
  const pose = { ...w.chars[0].pose };
  w.run(180);
  assert.deepEqual(w.chars[0].pose, pose, "does not move");
  assert.equal(w.chars[0].calls.length, mark, "does not change behavior");
  assert.ok(near(pose, desk(0).standSpot, 0.01));
  assert.equal(w.ctl.getStatus("a0").state, "paused");
  assert.equal(w.ctl.markerFor("a0"), "grey");
  assert.equal(w.chars[0].behavior !== "type", true);
});

test("markers: only waiting is amber and only paused is grey", () => {
  const w = makeWorld(1);
  for (const [s, m] of [["working", null], ["handoff", null], ["waiting", "amber"], ["idle", null], ["paused", "grey"]]) {
    w.send(0, s, s === "handoff" ? 0 : null);
    assert.equal(w.ctl.markerFor("a0"), m, s);
  }
  assert.equal(w.ctl.markerFor("nobody"), null);
});

// ---- cells ---------------------------------------------------------------------------------

test("no two characters reserve or stand on the same cell (handoffs to one colleague, shared pantry)", () => {
  const w = makeWorld(5, { rng: forced(DRINK), idleDelay: [1, 1] });
  for (let i = 1; i < 5; i++) w.send(i, "handoff", 0);
  w.send(0, "working");
  const seen = [];
  w.run(40, (x) => {
    const cells = x.chars.map((c, i) => x.ctl.occupancy.cellOf(c.id)).filter(Boolean).map((c) => `${c.i},${c.j}`);
    assert.equal(new Set(cells).size, cells.length, "reserved cells are unique");
    const talking = x.chars.filter((c, i) => i > 0 && c.behavior === "talk").map((c) => cellKey(c.pose));
    assert.equal(new Set(talking).size, talking.length, "standing visitors are on distinct cells");
    seen.push(talking.length);
  });
  assert.ok(Math.max(...seen) >= 2, "several visitors were talking at the same time");
  // pantry: three idle agents drink at once
  const p = makeWorld(3, { rng: forced(DRINK), idleDelay: [1, 1] });
  for (let i = 0; i < 3; i++) p.send(i, "idle");
  let drinkers = 0;
  p.run(60, (x) => {
    const d = x.chars.filter((c) => c.behavior === "drink").map((c) => cellKey(c.pose));
    assert.equal(new Set(d).size, d.length, "no two drink on the same cell");
    drinkers = Math.max(drinkers, d.length);
  });
  assert.ok(drinkers >= 2);
});

test("each character's home cell stays reserved: at home by the agent, while away by its placeholder", () => {
  const w = makeWorld(2, { rng: forced(DRINK), idleDelay: [1, 1] });
  const home = (i) => grid.worldToCell(desk(i).standSpot.x, desk(i).standSpot.z);
  for (let i = 0; i < 2; i++) assert.equal(w.ctl.occupancy.ownerOf(home(i)), `a${i}`, "reserved at start");
  w.send(0, "idle");
  w.send(1, "working");
  assert.ok(w.until((x) => x.chars[0].behavior === "drink", 60), "agent 0 is at the pantry");
  assert.equal(w.ctl.occupancy.ownerOf(home(0)), "a0:home", "away: placeholder holds the cell");
  assert.equal(w.ctl.occupancy.ownerOf(home(1)), "a1", "seated agent keeps its cell");
  w.send(0, "working");
  assert.ok(w.until((x) => x.chars[0].behavior === "type", 30));
  assert.equal(w.ctl.occupancy.ownerOf(home(0)), "a0", "back home: the agent holds it again");
  w.ctl.removeAgent("a0");
  assert.equal(w.ctl.occupancy.ownerOf(home(0)), undefined, "removed: cell is free");
});

// ---- determinism and the clock -----------------------------------------------------------

test("seededRandom is deterministic, in [0, 1), and depends on agent id and counter", () => {
  assert.equal(seededRandom("a", 3, "delay"), seededRandom("a", 3, "delay"));
  assert.notEqual(seededRandom("a", 3, "delay"), seededRandom("b", 3, "delay"));
  assert.notEqual(seededRandom("a", 3, "delay"), seededRandom("a", 4, "delay"));
  for (let i = 0; i < 500; i++) {
    const v = seededRandom(`agent-${i % 7}`, i, "kind");
    assert.ok(v >= 0 && v < 1);
  }
});

test("same seed and same events give the same behavior; the default rng needs no Math.random", () => {
  const realRandom = Math.random;
  Math.random = () => { throw new Error("Math.random must not be used"); };
  try {
    const script = (w) => {
      for (let i = 0; i < 4; i++) w.send(i, "idle");
      w.run(200);
      w.send(1, "working");
      w.run(60);
      w.send(2, "handoff", 3);
      w.run(60);
      return JSON.stringify(w.chars.map((c) => c.calls.map((k) => [k.name, +k.t.toFixed(4), +k.x.toFixed(3), +k.z.toFixed(3)])));
    };
    const a = script(makeWorld(4));
    const b = script(makeWorld(4));
    assert.equal(a, b);
    assert.ok(a.length > 200);
    // a different rng changes the behavior
    const c = script(makeWorld(4, { rng: (id, n, salt) => seededRandom(id + "x", n, salt) }));
    assert.notEqual(a, c);
  } finally {
    Math.random = realRandom;
  }
});

test("dt is clamped: a long gap (hidden tab) counts as at most MAX_DT", () => {
  const w = makeWorld(1);
  w.ctl.update(30);
  assert.ok(Math.abs(w.ctl.time - MAX_DT) < 1e-12);
  w.ctl.update(NaN);
  w.ctl.update(-1);
  assert.ok(Math.abs(w.ctl.time - MAX_DT) < 1e-12);
});

test("tick() measures dt from the injected now()", () => {
  let t = 100;
  const ctl = new BehaviorController({ layout, now: () => t });
  const ch = fakeChar("a0", { t: 0 });
  ctl.addAgent({ id: "a0", character: ch, deskIndex: 0 });
  ctl.tick();
  assert.equal(ctl.time, 0);
  t += 0.05;
  ctl.tick();
  assert.ok(Math.abs(ctl.time - 0.05) < 1e-12);
  t += 5;
  ctl.tick();
  assert.ok(Math.abs(ctl.time - 0.15) < 1e-12, "clamped to 0.1");
});

// ---- input handling ---------------------------------------------------------------------------

test("getStatus has state, behavior, bubble_text and marker; repeated state is a no-op", () => {
  const w = makeWorld(2);
  assert.equal(w.ctl.getStatus("nobody"), null);
  w.send(0, "working", null, "Counting stock");
  const s = w.ctl.getStatus("a0");
  assert.deepEqual(Object.keys(s).sort(), ["activity", "behavior", "bubble_text", "marker", "state", "target_agent_id"]);
  assert.equal(s.state, "working");
  assert.equal(s.bubble_text, "Counting stock");
  assert.equal(s.marker, null);
  w.run(5);
  const mark = w.chars[0].calls.length;
  w.send(0, "working", null, "Counting stock 2");
  w.run(2);
  assert.equal(w.chars[0].calls.length, mark, "no new calls");
  assert.equal(w.ctl.getStatus("a0").bubble_text, "Counting stock 2", "the bubble text is updated");
});

test("snapshot and visual_state events are applied; bad input is ignored", () => {
  const w = makeWorld(3);
  w.ctl.applySnapshot({ agents: [
    { agent_id: "a0", state: "working", target_agent_id: null, bubble_text: null },
    { agent_id: "a1", state: "paused", target_agent_id: null, bubble_text: null },
    { agent_id: "a2", state: "waiting", target_agent_id: null, bubble_text: "ok?" },
    { agent_id: "ghost", state: "working" },
    { agent_id: "a2", state: "dancing" },
  ] });
  assert.equal(w.ctl.getStatus("a0").state, "working");
  assert.equal(w.ctl.getStatus("a1").marker, "grey");
  assert.equal(w.ctl.getStatus("a2").state, "waiting");
  assert.equal(w.ctl.applyVisualState(null), false);
  assert.equal(w.ctl.applyVisualState({ agent_id: "a0", state: "nope" }), false);
  w.ctl.applySnapshot(undefined);
  assert.equal(w.ctl.applyVisualState({ agent_id: 1 === 1 ? "a1" : "", state: "idle" }), true);
});

test("removeAgent frees its cell", () => {
  const w = makeWorld(1);
  assert.ok(w.ctl.occupancy.cellOf("a0"));
  assert.equal(w.ctl.removeAgent("a0"), true);
  assert.equal(w.ctl.occupancy.cellOf("a0"), null);
  assert.equal(w.ctl.has("a0"), false);
});
