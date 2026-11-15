// Behavior controller: turns the backend visual state of each agent into motion.
// Pure logic, no Three.js import, so it runs in the browser and in Node tests.
//
// One small state machine per agent. A backend state (working, handoff, waiting,
// idle, paused) builds a "plan": a list of steps (stand up, walk, turn, sit, talk,
// wave, ...). The steps run one after the other. A new backend state throws the
// plan away and builds a new one from wherever the character is, so it always
// interrupts an idle behavior at once (Req 2.7).
//
// Truthfulness (Req 2.8): the `type` behavior is requested only by the sit step
// of the `working` plan. Idle behaviors (stretch, drink, chat) never use it, and
// every other state stands up first, so typing stops the moment the backend
// leaves `working`.
//
// Interfaces (so tests can inject fakes):
//   character: { id, setBehavior(name, opts), setPose?(x, z, yaw) }
//     `setPose` is optional; without it the controller writes to
//     `character.object3D.position` and `character.object3D.rotation.y`.
//   layout:    the layout.js export (desks, pantrySpot, grid, ...)
//   rng:       (agentId, counter, salt) => number in [0, 1). Deterministic by default.
//   now:       () => seconds, only used by `tick()`.
//
// Movement uses pathfinding.js (A*) and walker.js (1.2 m/s, eased turning, the
// occupied-cell rule). Yaw convention is the one of layout.js: yaw 0 faces +z.

import { layout as defaultLayout } from "./layout.js";
import { Grid } from "./pathfinding.js";
import { Walker, OccupancyMap, walkTo, angleDelta, normalizeAngle, WALK_SPEED, MAX_TURN_RATE } from "./walker.js";

export const STATES = Object.freeze(["working", "handoff", "waiting", "idle", "paused"]);
export const MARKERS = Object.freeze({ waiting: "amber", paused: "grey" });

/** Largest dt one update accepts, in seconds. Longer gaps (hidden tab) count as this. */
export const MAX_DT = 0.1;

/** A seated character's root is this far in front of the seat (see characters.js seatedRoot). */
export const SEAT_PELVIS_BACK = 0.24;

export const TIMING = Object.freeze({
  sit: 1.3,              // sit_down clip (1.30 s), until sit_idle takes over
  sitSlide: 1.0,         // the root slides from the stand spot to the seat during the first part of it
  stand: 1.0,            // stand_up clip (1.03 s), and the slide back to the stand spot
  stretch: 3,
  drink: 3,
  talkMin: 3,            // handoff and chat talk 3 to 5 s
  talkMax: 5,
  idleMin: 20,           // an idle agent starts a behavior every 20 to 60 s
  idleMax: 60,
  chatWait: 10,          // give up waiting for a chat partner after this long
  walkGiveUp: 6,         // give up on a target that stays occupied or unreachable
  yieldGrace: 0.8,       // after planning around a blocker, do not check for blockers for this long
  yieldBudget: 3,        // and stops yielding after this many seconds per walk
  personalSpace: 0.5,    // a path keeps its cell centers at least this far from the other characters
  yieldDistance: 0.6,    // another character this close, in front of us, is "in the way"
  arriveDistance: 0.35,  // "at the stand spot" when closer than this
});

// Two pairs of spots where idle agents chat, on both sides of the meeting table.
// Each pair faces each other 1 m apart. Cells are free in layout.js (checked by the tests).
export const CHAT_SPOTS = Object.freeze([
  Object.freeze([
    Object.freeze({ x: -3.25, z: 3.25, rotation: Math.PI / 2 }),
    Object.freeze({ x: -2.25, z: 3.25, rotation: -Math.PI / 2 }),
  ]),
  Object.freeze([
    Object.freeze({ x: 2.25, z: 3.25, rotation: Math.PI / 2 }),
    Object.freeze({ x: 3.25, z: 3.25, rotation: -Math.PI / 2 }),
  ]),
]);

// ---- deterministic randomness ------------------------------------------------------

function hashString(str) {
  let h = 2166136261 >>> 0;   // FNV-1a
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h;
}

/** Number in [0, 1) that depends only on (agentId, counter, salt). */
export function seededRandom(agentId, counter = 0, salt = "") {
  let a = (hashString(`${agentId}|${counter}|${salt}`) + 0x6d2b79f5) >>> 0;
  let t = a;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}

// ---- small helpers -------------------------------------------------------------------

const ease = (t) => { const x = Math.min(1, Math.max(0, t)); return x * x * (3 - 2 * x); };
const lerp = (a, b, t) => a + (b - a) * t;
const dist = (a, b) => Math.hypot(a.x - b.x, a.z - b.z);

function setCharacterPose(ch, x, z, yaw) {
  if (typeof ch.setPose === "function") ch.setPose(x, z, yaw);
  else if (ch.object3D) {
    ch.object3D.position.set(x, 0, z);
    ch.object3D.rotation.y = yaw;
  }
}

// Yaw that makes `from` look at `to`'s current position. Falls back to `fallback`
// when they stand (almost) on top of each other.
function faceYaw(from, to, fallback) {
  const p = to.pose();
  const dx = p.x - from.walker.x;
  const dz = p.z - from.walker.z;
  return Math.hypot(dx, dz) < 0.3 ? fallback : Math.atan2(dx, dz);
}

/** Where a seated character's root goes (same formula as characters.js seatedRoot). */
export function seatRoot(desk) {
  return {
    x: desk.seat.x + Math.sin(desk.rotation) * SEAT_PELVIS_BACK,
    z: desk.seat.z + Math.cos(desk.rotation) * SEAT_PELVIS_BACK,
  };
}

// ---- steps ---------------------------------------------------------------------------
// A step: { kind, label, setsBehavior?, start(b), update(b, dt) -> done, end?(b), cancel?(b) }
// `label` is the behavior name shown by getStatus. `b` is the AgentBrain.

// Walk to a world point. Reserves the goal cell (occupancy) and plays the walk clip while moving.
// Plans around cells other characters hold or stand on. If a character is in the way while
// walking, it plans again around that character, or waits for it when the other walker has
// priority. It gives up waiting after TIMING.yieldBudget seconds so two walkers cannot deadlock.
function walkStep(goal) {
  let target = goal;   // the goal, or the free neighbor cell walkTo chose instead
  let waited = 0;
  let planned = false;
  let stall = 0;       // > 0 while standing still for another walker
  let yielded = 0;     // seconds spent waiting for others in this walk
  let grace = 0;       // after planning around a blocker, do not look for blockers for a moment
  return {
    kind: "walk",
    label: "walking",
    setsBehavior: true,
    start(b) { this.try(b, 0); },
    try(b, dt) {
      b.releaseHomeIfGoal(target);
      // Plan around cells other characters hold. If that leaves no way, plan on the bare floor.
      const now = { now: b.ctl.time };
      let r = walkTo(b.walker, b.ctl.gridAvoidingOthers(b.id, target), b.ctl.occupancy, b.id, target, now);
      if (r.status === "unreachable") r = walkTo(b.walker, b.ctl.grid, b.ctl.occupancy, b.id, target, now);
      b.syncHome();
      // If the goal cell was taken, walkTo chose a free neighbor: plan again toward that one.
      if (r.status === "redirected" && r.path?.length) target = r.path[r.path.length - 1];
      if (r.status === "waiting") {            // target cell is taken: stand and retry
        waited += dt;
        if (b.charBehavior !== "idle") b.setBehavior("idle");
        return;
      }
      planned = true;
      if (r.status === "unreachable") {        // give up on this walk, stay where we are
        b.walker.stop();
        b.setBehavior("idle");
        return;
      }
      if (b.walker.moving) b.setBehavior("walk", { speed: b.walker.speed });
      else b.setBehavior("idle");
    },
    update(b, dt) {
      if (!planned) {
        this.try(b, dt);
        if (!planned) return waited >= TIMING.walkGiveUp;
      }
      const blocker = grace <= 0 && yielded < TIMING.yieldBudget && b.walker.moving ? b.ctl.blockerAhead(b, target) : null;
      if (grace > 0) grace -= dt;
      if (blocker) {
        // Head-on with another walker: the one with the smaller id goes first, the other waits.
        // Otherwise (the other stands still, or has to wait for us) plan around it right away.
        if (blocker.plan[0]?.kind === "walk" && blocker.id < b.id) {
          if (stall === 0) b.setBehavior("idle");
          stall += dt || 1e-6;
          yielded += dt;
          return false;
        }
        this.try(b, 0);
        grace = TIMING.yieldGrace;
      }
      if (stall > 0) {
        stall = 0;
        if (b.walker.moving) b.setBehavior("walk", { speed: b.walker.speed });
      }
      b.walker.update(dt);
      b.syncFromWalker();
      if (b.walker.arrived || !b.walker.moving) {
        b.walker.arrived = true;
        return true;
      }
      return false;
    },
    end(b) {
      b.walker.stop();
      b.setBehavior("idle");
    },
  };
}

// Turn in place toward `target` (eased), standing. `target` is a yaw, or a function
// (brain) => yaw that is read every frame (used to face a colleague who may move).
function turnStep(target) {
  return {
    kind: "turn",
    label: "standing",
    start() {},
    update(b, dt) {
      const w = b.walker;
      const yaw = typeof target === "function" ? target(b) : target;
      const d = angleDelta(yaw, w.yaw);
      if (Math.abs(d) < 0.02) {
        w.setPose(w.x, w.z, yaw);
        b.syncFromWalker();
        return true;
      }
      const rate = Math.min(MAX_TURN_RATE, Math.abs(d) * 8 + 0.5);
      const move = Math.min(Math.abs(d), rate * dt);
      w.setPose(w.x, w.z, w.yaw + Math.sign(d) * move);
      b.syncFromWalker();
      return false;
    },
  };
}

// Stand up from the seat and slide back to the stand spot.
function standUpStep() {
  let elapsed = 0;
  let from = 1;
  let span = TIMING.stand;
  return {
    kind: "standUp",
    label: "standing_up",
    setsBehavior: true,
    start(b) {
      from = b.slide;
      span = Math.max(0.3, TIMING.stand * from);
      b.setBehavior("stand");
    },
    update(b, dt) {
      elapsed += dt;
      b.slide = from * (1 - Math.min(1, elapsed / span));
      b.applyPose();
      return elapsed >= span;
    },
    end(b) {
      b.slide = 0;
      b.walker.setPose(b.desk.standSpot.x, b.desk.standSpot.z, b.desk.rotation);
      b.syncFromWalker();
    },
  };
}

// Sit down at the desk (stand spot -> seat) and start typing. Only `working` plans use it.
function sitStep() {
  let elapsed = 0;
  return {
    kind: "sit",
    get label() { return elapsed >= TIMING.sit ? "typing" : "sitting_down"; },
    setsBehavior: true,
    start(b) {
      if (b.slide === 0 && dist(b.walker, b.desk.standSpot) > TIMING.arriveDistance) {
        b.walker.setPose(b.desk.standSpot.x, b.desk.standSpot.z, b.desk.rotation);   // never show work away from the desk
      }
      if (b.slide === 0) b.walker.setPose(b.walker.x, b.walker.z, b.desk.rotation);
      elapsed = b.slide >= 1 ? TIMING.sit : b.slide * TIMING.sitSlide;
      b.releaseHomeIfGoal(b.desk.standSpot);
      b.ctl.occupancy.reserve(b.homeCell, b.id);
      b.syncHome();
      b.setBehavior("type");
    },
    update(b, dt) {
      elapsed += dt;
      b.slide = Math.min(1, elapsed / TIMING.sitSlide);
      b.applyPose();
      return elapsed >= TIMING.sit;
    },
  };
}

// Stay where we are until the plan is replaced.
function holdStep(label) {
  return { kind: "hold", label, start() {}, update() { return false; } };
}

// Play a timed procedural gesture (wave, stretch, drink) while standing.
function gestureStep(name, label, seconds) {
  let elapsed = 0;
  return {
    kind: name,
    label,
    setsBehavior: true,
    start(b) { b.setBehavior(name, { duration: seconds }); },
    update(b, dt) {
      elapsed += dt;
      return elapsed >= seconds + 0.05;
    },
  };
}

// Keep a procedural gesture going until the plan is replaced (the raised hand of a waiting
// agent, Req 2.6). Duration Infinity makes the character skip its 3 s fall-back to idle.
function holdGestureStep(name, label) {
  return {
    kind: name,
    label,
    setsBehavior: true,
    start(b) { b.setBehavior(name, { duration: Infinity }); },
    update() { return false; },
  };
}

// Talk while standing for `seconds`. With a chat `session`, stop early when it is cancelled
// or when the rendezvous did not happen.
function talkStep(seconds, session = null) {
  let elapsed = 0;
  return {
    kind: "talk",
    label: "talking",
    setsBehavior: true,
    start(b) {
      if (session && !session.ok) { elapsed = seconds; return; }
      b.setBehavior("talk");
    },
    update(b, dt) {
      elapsed += dt;
      if (session && session.cancelled) return true;
      return elapsed >= seconds;
    },
    end(b) { b.setBehavior("idle"); },
    cancel() { if (session) session.cancelled = true; },
  };
}

// Chat rendezvous: stand at the chat spot until the partner is there too.
function rendezvousStep(session) {
  let waited = 0;
  return {
    kind: "rendezvous",
    label: "standing",
    start(b) { session.arrived.add(b.id); },
    update(b, dt) {
      if (session.cancelled) { session.ok = false; return true; }
      if (session.arrived.size >= 2) { session.ok = true; return true; }
      waited += dt;
      if (waited >= TIMING.chatWait) { session.cancelled = true; session.ok = false; return true; }
      return false;
    },
    cancel() { session.cancelled = true; },
  };
}

function clearActivityStep() {
  return { kind: "settle", label: "standing", start(b) { b.activity = null; }, update: () => true };
}

// Idle: wait 20 to 60 s (seeded), then start one idle behavior and come back here.
function idleLoopStep() {
  let due = 0;
  return {
    kind: "idleLoop",
    label: "standing",
    idleLoop: true,
    start(b) { due = b.ctl.time + b.ctl.idleDelay(b); },
    update(b) {
      if (b.ctl.time < due) return false;
      b.ctl.startIdleBehavior(b);
      return false;
    },
  };
}

// ---- one agent -----------------------------------------------------------------------

class AgentBrain {
  constructor(ctl, { id, character, desk }) {
    this.ctl = ctl;
    this.id = id;
    this.char = character;
    this.desk = desk;
    this.walker = new Walker({ x: desk.standSpot.x, z: desk.standSpot.z, yaw: desk.rotation, speed: ctl.walkSpeed });
    this.homeCell = ctl.grid.worldToCell(desk.standSpot.x, desk.standSpot.z);
    this.slide = 0;               // 0 standing at the stand spot ... 1 seated
    this.state = "idle";
    this.target = null;
    this.bubble_text = null;
    this.activity = null;         // idle behavior in progress: stretch | drink | chat
    this.idleCount = 0;           // counter for the seeded idle choices
    this.fresh = true;            // no backend state seen yet
    this.plan = [];
    this.charBehavior = null;
    this._pose = null;
    this.homeId = `${id}:home`;   // keeps the home cell reserved while the agent is away
    ctl.occupancy.reserve(this.homeCell, id);
    this.applyPose();
  }

  // The occupancy map holds one cell per id. While the agent stands elsewhere, a second
  // id (`<id>:home`) keeps its home cell reserved, so nobody else is sent to its desk.
  syncHome() {
    const occ = this.ctl.occupancy;
    const c = occ.cellOf(this.id);
    if (c && c.i === this.homeCell.i && c.j === this.homeCell.j) occ.release(this.homeId);
    else occ.reserve(this.homeCell, this.homeId);
  }

  // Before walking to a goal inside the home cell, give up the placeholder (it would block us).
  releaseHomeIfGoal(goal) {
    const c = this.ctl.grid.worldToCell(goal.x, goal.z);
    if (c.i === this.homeCell.i && c.j === this.homeCell.j) this.ctl.occupancy.release(this.homeId);
  }

  get label() { return this.plan[0]?.label ?? "standing"; }

  /** Current { x, z } of the character (on the sit slide, or at the walker). */
  pose() {
    return this._pose ?? { x: this.walker.x, z: this.walker.z };
  }

  setBehavior(name, opts) {
    this.charBehavior = name;
    this.char.setBehavior(name, opts);
  }

  away() {
    return this.slide === 0 && dist(this.walker, this.desk.standSpot) > TIMING.arriveDistance;
  }

  // Place the character: on the slide between stand spot and seat, or at the walker.
  applyPose() {
    let x;
    let z;
    let yaw;
    if (this.slide > 0) {
      const root = seatRoot(this.desk);
      const t = ease(this.slide);
      x = lerp(this.desk.standSpot.x, root.x, t);
      z = lerp(this.desk.standSpot.z, root.z, t);
      yaw = this.desk.rotation;
    } else {
      x = this.walker.x; z = this.walker.z; yaw = this.walker.yaw;
    }
    const p = this._pose;
    if (p && p.x === x && p.z === z && p.yaw === yaw) return;
    this._pose = { x, z, yaw };
    setCharacterPose(this.char, x, z, yaw);
  }

  syncFromWalker() { this.applyPose(); }

  // Replace the plan. Steps of the old plan that are not kept get `cancel`.
  setPlan(steps, { keep = null } = {}) {
    for (const old of this.plan) if (old !== keep) old.cancel?.(this);
    this.walker.stop();
    this.plan = steps;
    // Stop whatever gesture or walk is playing, unless the first new step sets its own
    // behavior (or an unfinished talk goes on). Seated characters are handled by their
    // first step (stand up or sit).
    if (this.slide === 0 && !keep && !steps[0]?.setsBehavior) this.setBehavior("idle");
    this.advance(0);
  }

  advance(dt) {
    let guard = 0;
    while (this.plan.length && guard++ < 32) {
      const step = this.plan[0];
      if (!step.started) {
        step.started = true;
        step.start(this);
        if (this.plan[0] !== step) { dt = 0; continue; }
      }
      const done = step.update(this, dt);
      if (this.plan[0] !== step) { dt = 0; continue; }   // plan replaced during the update
      if (!done) break;
      this.plan.shift();
      step.end?.(this);
      dt = 0;
    }
  }

  // Steps that bring the character to its stand spot, facing the desk, standing.
  homeSteps() {
    const steps = [];
    if (this.slide > 0) steps.push(standUpStep());
    else if (this.away()) steps.push(walkStep(this.desk.standSpot));
    steps.push(turnStep(this.desk.rotation));
    return steps;
  }
}

// ---- controller ----------------------------------------------------------------------

export class BehaviorController {
  /**
   * @param {object} [o]
   * @param {object} [o.layout]      layout.js export
   * @param {Function} [o.rng]       (agentId, counter, salt) => [0, 1)
   * @param {Function} [o.now]       () => seconds, used by tick()
   * @param {number[]} [o.idleDelay] [min, max] seconds between idle behaviors (default 20 to 60)
   * @param {number} [o.walkSpeed]   m/s (default 1.2)
   * @param {object[][]} [o.chatSpots] pairs of {x, z, rotation}
   */
  constructor({ layout = defaultLayout, rng = seededRandom, now = () => globalThis.performance.now() / 1000,
    idleDelay = [TIMING.idleMin, TIMING.idleMax], walkSpeed = WALK_SPEED, chatSpots = CHAT_SPOTS } = {}) {
    this.layout = layout;
    this.grid = new Grid(layout);
    this.rng = rng;
    this.now = now;
    this.idleRange = idleDelay;
    this.walkSpeed = walkSpeed;
    this.chatSpots = chatSpots;
    this.time = 0;
    this.occupancy = new OccupancyMap(() => this.time);
    this.brains = new Map();
    this._lastTick = null;
  }

  // The floor grid plus the cells other characters stand on or walk to, so a path never
  // goes through a colleague who is standing still (the occupancy map is not a wall in
  // plain A*). Built per walk: the walk planner only calls isBlocked / worldToCell / cellToWorld.
  gridAvoidingOthers(id, goal = null) {
    const base = this.grid;
    const occ = this.occupancy;
    const goalCell = goal ? base.worldToCell(goal.x, goal.z) : null;
    // where the others stand right now (a walker does not hold the cell it is crossing)
    const others = [];
    for (const o of this.brains.values()) if (o.id !== id) others.push(o.pose());
    const near = (i, j) => {
      if (goalCell && i === goalCell.i && j === goalCell.j) return false;
      const c = base.cellToWorld(i, j);
      return others.some((p) => Math.hypot(p.x - c.x, p.z - c.z) < TIMING.personalSpace);
    };
    const g = Object.create(base);
    g.isBlocked = (i, j) => base.isBlocked(i, j) || !occ.isFree({ i, j }, id) || near(i, j);
    return g;
  }

  /** The brain of a character, not seated, that is right ahead of `b` on its way (and `b` is not about to arrive), or null. */
  blockerAhead(b, goal) {
    if (Math.hypot(goal.x - b.walker.x, goal.z - b.walker.z) < 0.7) return null;
    const fx = Math.sin(b.walker.yaw);
    const fz = Math.cos(b.walker.yaw);
    for (const o of this.brains.values()) {
      if (o === b || o.slide > 0) continue;
      const p = o.pose();
      const dx = p.x - b.walker.x;
      const dz = p.z - b.walker.z;
      const d = Math.hypot(dx, dz);
      if (d < TIMING.yieldDistance && d > 1e-6 && (dx * fx + dz * fz) / d > 0.3) return o;
    }
    return null;
  }

  // ---- agents ----

  /** Register an agent. The character starts standing at its stand spot, facing the desk. */
  addAgent({ id, character, deskIndex, desk = null }) {
    const key = String(id);
    if (this.brains.has(key)) throw new Error(`agent "${key}" is already registered`);
    const d = desk ?? this.layout.deskForAgent(deskIndex);
    const b = new AgentBrain(this, { id: key, character, desk: d });
    this.brains.set(key, b);
    b.setPlan([turnStep(d.rotation), idleLoopStep()]);
    return b;
  }

  removeAgent(id) {
    const b = this.brains.get(String(id));
    if (!b) return false;
    for (const s of b.plan) s.cancel?.(b);
    this.occupancy.release(b.id);
    this.occupancy.release(b.homeId);
    this.brains.delete(b.id);
    return true;
  }

  has(id) { return this.brains.has(String(id)); }
  agentIds() { return [...this.brains.keys()]; }

  // ---- backend input ----

  /** Feed one `visual_state` event (or one entry of a snapshot). Returns false if ignored. */
  applyVisualState(evt) {
    if (!evt || evt.agent_id == null) return false;
    const b = this.brains.get(String(evt.agent_id));
    if (!b || !STATES.includes(evt.state)) return false;
    const target = evt.state === "handoff" && evt.target_agent_id != null ? String(evt.target_agent_id) : null;
    b.bubble_text = evt.bubble_text ?? null;
    if (b.state === evt.state && b.target === target && !b.fresh) return true;   // same state: nothing to redo
    const previous = b.state;
    const wasFresh = b.fresh;
    b.fresh = false;
    b.state = evt.state;
    b.target = target;
    b.activity = null;
    if (wasFresh && evt.state === "working") {
      b.slide = 1;                     // page load: start already seated, no walk
      b.applyPose();
    }
    this._plan(b, previous);
    return true;
  }

  /** Feed a `snapshot` event: { agents: [{agent_id, state, target_agent_id, bubble_text}] }. */
  applySnapshot(snapshot) {
    for (const entry of snapshot?.agents ?? []) this.applyVisualState(entry);
  }

  _plan(b, previous) {
    let keepTalk = null;
    // A handoff conversation that has started may finish when the backend goes idle.
    if (previous === "handoff" && b.state === "idle") {
      const cur = b.plan[0];
      if (cur && cur.kind === "talk" && cur.started && !cur.session) keepTalk = cur;
    }
    switch (b.state) {
      case "working": {
        const steps = [];
        if (b.slide === 0 && b.away()) steps.push(walkStep(b.desk.standSpot));
        if (b.slide === 0) steps.push(turnStep(b.desk.rotation));
        steps.push(sitStep(), holdStep("typing"));
        b.setPlan(steps);
        break;
      }
      case "handoff": {
        const other = b.target && b.target !== b.id ? this.brains.get(b.target) : null;
        const steps = [];
        if (b.slide > 0) steps.push(standUpStep());
        if (other) {
          const v = other.desk.visitSpot;
          steps.push(walkStep(v), turnStep((me) => faceYaw(me, other, v.rotation)), talkStep(this.talkSeconds(b)), walkStep(b.desk.standSpot), turnStep(b.desk.rotation), holdStep("standing"));
        } else {
          // Unknown target: talk at our own desk instead of inventing a walk.
          if (b.slide === 0 && b.away()) steps.push(walkStep(b.desk.standSpot));
          steps.push(turnStep(b.desk.rotation), talkStep(this.talkSeconds(b)), holdStep("standing"));
        }
        b.setPlan(steps);
        break;
      }
      case "waiting":
        // Stand at the desk with the hand up for as long as the backend says waiting.
        b.setPlan([...b.homeSteps(), holdGestureStep("wave", "waving")]);
        break;
      case "paused":
        b.setPlan([...b.homeSteps(), holdStep("standing")]);
        break;
      case "idle":
      default: {
        const tail = [...b.homeSteps(), idleLoopStep()];
        if (keepTalk) b.setPlan([keepTalk, ...tail], { keep: keepTalk });
        else b.setPlan(tail);
        break;
      }
    }
  }

  // ---- idle behaviors ----

  idleDelay(b) {
    const [lo, hi] = this.idleRange;
    return lo + (hi - lo) * this.rng(b.id, b.idleCount, "delay");
  }

  talkSeconds(b) {
    return TIMING.talkMin + (TIMING.talkMax - TIMING.talkMin) * this.rng(b.id, b.idleCount, "talk");
  }

  _chatPair(b) {
    const n = this.chatSpots.length;
    if (!n) return null;
    const start = Math.floor(this.rng(b.id, b.idleCount, "pair") * n) % n;
    for (let k = 0; k < n; k++) {
      const pair = this.chatSpots[(start + k) % n];
      const free = pair.every((s) => this.occupancy.isFree(this.grid.worldToCell(s.x, s.z), b.id));
      if (free) return pair;
    }
    return null;
  }

  _chatPartner(b) {
    const candidates = [];
    for (const o of this.brains.values()) {
      if (o === b || o.state !== "idle" || o.slide !== 0 || o.away()) continue;
      const cur = o.plan[0];
      if (cur && cur.idleLoop && cur.started) candidates.push(o);
    }
    if (!candidates.length) return null;
    return candidates[Math.floor(this.rng(b.id, b.idleCount, "partner") * candidates.length) % candidates.length];
  }

  /** Pick and start one idle behavior for `b` (called by its idle loop when the timer is due). */
  startIdleBehavior(b) {
    const n = b.idleCount;
    const partner = this._chatPartner(b);
    const pair = partner ? this._chatPair(b) : null;
    const kinds = ["stretch", "drink"];
    if (partner && pair) kinds.push("chat");
    const kind = kinds[Math.floor(this.rng(b.id, n, "kind") * kinds.length) % kinds.length];
    b.idleCount++;
    b.activity = kind;
    // After the behavior: walk back to the desk, face it, clear the activity, wait for the next one.
    const back = (who) => [walkStep(who.desk.standSpot), turnStep(who.desk.rotation), ...settle()];
    const settle = () => [clearActivityStep(), idleLoopStep()];
    if (kind === "stretch") {
      b.setPlan([gestureStep("stretch", "stretching", TIMING.stretch), ...settle()]);
    } else if (kind === "drink") {
      const p = this.layout.pantrySpot;
      b.setPlan([walkStep(p), turnStep(p.rotation), gestureStep("drink", "drinking", TIMING.drink), ...back(b)]);
    } else {
      partner.idleCount++;
      partner.activity = "chat";
      const session = { arrived: new Set(), ok: null, cancelled: false };
      const seconds = this.talkSeconds(b);
      const route = (who, spot) => [
        walkStep(spot), turnStep(spot.rotation), rendezvousStep(session),
        Object.assign(talkStep(seconds, session), { session }),
        ...back(who),
      ];
      // Give each agent the spot that keeps the two walks from crossing (shorter total distance).
      const from = (who) => ({ x: who.walker.x, z: who.walker.z });
      const straight = dist(from(b), pair[0]) + dist(from(partner), pair[1]);
      const swapped = dist(from(b), pair[1]) + dist(from(partner), pair[0]);
      const [mine, theirs] = swapped < straight ? [pair[1], pair[0]] : [pair[0], pair[1]];
      partner.setPlan(route(partner, theirs));
      b.setPlan(route(b, mine));
    }
  }

  // ---- per frame ----

  /** Advance every agent by `dt` seconds (clamped to MAX_DT). Call before the characters' own update. */
  update(dt) {
    if (!Number.isFinite(dt) || dt <= 0) dt = 0;
    dt = Math.min(dt, MAX_DT);
    this.time += dt;
    for (const b of this.brains.values()) b.advance(dt);
  }

  /** update() with dt measured from the injected `now`. */
  tick() {
    const t = this.now();
    const dt = this._lastTick == null ? 0 : t - this._lastTick;
    this._lastTick = t;
    this.update(dt);
  }

  // ---- read side (task 8.7 draws bubbles and markers from this) ----

  markerFor(agentId) {
    const b = this.brains.get(String(agentId));
    return b ? MARKERS[b.state] ?? null : null;
  }

  getStatus(agentId) {
    const b = this.brains.get(String(agentId));
    if (!b) return null;
    return {
      state: b.state,
      behavior: b.label,
      bubble_text: b.bubble_text,
      marker: MARKERS[b.state] ?? null,
      target_agent_id: b.target,
      activity: b.activity,
    };
  }

  /** Current pose of an agent: { x, z, yaw, seated }. */
  poseOf(agentId) {
    const b = this.brains.get(String(agentId));
    if (!b) return null;
    const p = b._pose ?? { x: b.walker.x, z: b.walker.z, yaw: b.walker.yaw };
    return { x: p.x, z: p.z, yaw: normalizeAngle(p.yaw), seated: b.slide > 0 };
  }
}
