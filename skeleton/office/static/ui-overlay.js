// HTML overlay on top of the 3D scene: speech bubble, status marker and name label for each agent,
// plus click picking (raycast on the character meshes).
//
// Everything here is DOM, not WebGL text, so it stays sharp and is readable by screen readers.
// Each frame the head position of every character is projected to screen space and the tag is moved there.
// All text goes in with textContent. Nothing from the server is ever parsed as HTML.

import * as THREE from "three";
import { BUBBLE_MAX_CHARS, bubbleText, stateColor, stateDetail } from "./ui-format.js";

const HEAD_LIFT = 0.3;          // meters above the Head bone: the top of the head, plus a little air
const SMOOTHING = 14;           // 1/s: how fast a tag follows the head (hides the bobbing of the walk cycle)
const OFFSCREEN_MARGIN = 24;    // px a tag may be outside the screen before it hides
const TOP_GAP = 6;              // px of space kept between the top bar and a tag
const CLICK_MOVE_PX = 6;

const _v = new THREE.Vector3();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * @param opts.container   the overlay element (position: fixed, pointer-events: none)
 * @param opts.office      createOffice() result (camera, renderer)
 * @param opts.controller  BehaviorController (getStatus, markerFor)
 * @param opts.characters  Map agent id -> character (from characters.js)
 * @param opts.agentInfo   (id) => { name, role } | undefined
 * @param opts.topBar      element whose bottom edge the tags must stay below
 * @param opts.onSelect    (id) => void, called for a label click or a click on a character
 */
export function createOverlay({ container, office, controller, characters, agentInfo, topBar, onSelect }) {
  const tags = new Map();   // agent id -> tag record
  let width = window.innerWidth;
  let height = window.innerHeight;
  let barBottom = 0;
  let selectedId = null;

  function measure() {
    width = container.clientWidth || window.innerWidth;
    height = container.clientHeight || window.innerHeight;
    barBottom = topBar ? topBar.getBoundingClientRect().bottom : 0;
  }
  measure();
  const resizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
  resizeObserver?.observe(container);
  if (topBar) resizeObserver?.observe(topBar);
  window.addEventListener("resize", measure);

  function createTag(id) {
    const root = el("div", "agent-tag");
    root.dataset.agentId = id;
    const inner = el("div", "tag-inner");
    const bubble = el("div", "bubble");
    bubble.setAttribute("aria-hidden", "true"); // the same text is in the label's description and in the panel
    const row = el("div", "tag-row");
    const marker = el("span", "marker marker-green");
    marker.setAttribute("aria-hidden", "true");
    const button = el("button", "agent-label");
    button.type = "button";
    button.addEventListener("click", () => onSelect?.(id));
    row.append(marker, button);
    inner.append(bubble, row);
    root.append(inner);
    container.append(root);
    return {
      id, root, inner, bubble, row, marker, button,
      world: new THREE.Vector3(), placed: false,
      shown: false, bubbleShown: false,
      text: null, color: null, label: null, name: null, bubbleH: 0, rowH: 0,
    };
  }

  function removeTag(tag) {
    tag.root.remove();
    tags.delete(tag.id);
  }

  function updateContent(tag, status, info) {
    const name = info?.name ?? tag.id;
    const color = status ? controller.markerFor(tag.id) ?? stateColor(status.state) : "grey";
    const text = bubbleText(status?.bubble_text);
    const detail = status ? stateDetail(status.state, status.activity) : "Unknown";

    if (name !== tag.name) {
      tag.name = name;
      tag.button.textContent = name;
      tag.rowH = 0;
    }
    if (color !== tag.color) {
      tag.color = color;
      tag.marker.className = `marker marker-${color}`;
    }
    const label = `${name}${info?.role ? `, ${info.role}` : ""}. ${detail}.${text ? ` ${text}` : ""} Open details`;
    if (label !== tag.label) {
      tag.label = label;
      tag.button.setAttribute("aria-label", label);
      tag.marker.title = detail;
    }
    // Keep the old words while the bubble fades out. Only replace them when there is something to show.
    if (text && text !== tag.text) {
      tag.text = text;
      tag.bubble.textContent = text;
      const full = String(status.bubble_text).trim();
      if (full.length > BUBBLE_MAX_CHARS || full !== text) tag.bubble.title = full;
      else tag.bubble.removeAttribute("title");
      tag.bubbleH = 0;
    }
    return text;
  }

  function setShown(tag, shown, bubbleShown) {
    if (shown !== tag.shown) {
      tag.shown = shown;
      tag.root.classList.toggle("is-visible", shown);
    }
    const b = shown && bubbleShown;
    if (b !== tag.bubbleShown) {
      tag.bubbleShown = b;
      tag.bubble.classList.toggle("is-visible", b);
    }
  }

  function update(dt) {
    // Create tags for new characters, drop tags of removed ones.
    for (const id of characters.keys()) if (!tags.has(id)) tags.set(id, createTag(id));
    for (const tag of [...tags.values()]) if (!characters.has(tag.id)) removeTag(tag);
    if (!tags.size) return;

    const camera = office.camera;
    camera.updateMatrixWorld();
    const k = 1 - Math.exp(-SMOOTHING * Math.min(dt, 0.1));

    for (const tag of tags.values()) {
      const character = characters.get(tag.id);
      const status = controller.getStatus(tag.id);
      const text = updateContent(tag, status, agentInfo?.(tag.id));

      // Head position in the world, smoothed.
      character.object3D.updateMatrixWorld(true);
      const head = character.bones?.Head;
      if (head) head.getWorldPosition(_v);
      else character.object3D.getWorldPosition(_v).y += 1.7;
      _v.y += HEAD_LIFT;
      if (!tag.placed) { tag.world.copy(_v); tag.placed = true; }
      else tag.world.lerp(_v, k);

      _v.copy(tag.world).project(camera);
      const sx = (_v.x * 0.5 + 0.5) * width;
      const sy = (-_v.y * 0.5 + 0.5) * height;
      tag.root.style.transform = `translate3d(${sx.toFixed(1)}px, ${sy.toFixed(1)}px, 0)`;

      // Measure sizes only when the content changed (reading layout every frame would be wasteful).
      if (!tag.rowH) tag.rowH = tag.inner.offsetHeight; // the bubble is absolutely positioned, so not counted
      if (!tag.bubbleH) tag.bubbleH = tag.bubble.offsetHeight;

      const inFront = _v.z > -1 && _v.z < 1;
      const onScreen = inFront && sx > -OFFSCREEN_MARGIN && sx < width + OFFSCREEN_MARGIN && sy < height + OFFSCREEN_MARGIN;
      const rowTop = sy - tag.rowH;
      const clearOfBar = rowTop >= barBottom + TOP_GAP;
      const bubbleTop = rowTop - tag.bubbleH - 8;
      const bubbleClear = bubbleTop >= barBottom + TOP_GAP;
      setShown(tag, onScreen && clearOfBar, text !== "" && bubbleClear);
    }
  }

  function setSelected(id) {
    selectedId = id == null ? null : String(id);
    for (const tag of tags.values()) tag.root.classList.toggle("selected", tag.id === selectedId);
  }

  /** Focus the label of an agent (used after the panel closes). */
  function focusLabel(id) {
    tags.get(String(id))?.button.focus({ preventScroll: true });
  }

  // ---- picking ----

  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();

  /** Agent id under a screen point, or null. Raycasts against the character meshes only. */
  function pick(clientX, clientY) {
    const rect = office.renderer.domElement.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    ndc.set(((clientX - rect.left) / rect.width) * 2 - 1, -(((clientY - rect.top) / rect.height) * 2 - 1));
    office.camera.updateMatrixWorld();
    raycaster.setFromCamera(ndc, office.camera);
    const roots = [];
    const owner = new Map();
    for (const [id, c] of characters) {
      c.object3D.updateMatrixWorld(true);
      roots.push(c.object3D);
      owner.set(c.object3D, id);
    }
    const hits = raycaster.intersectObjects(roots, true);
    for (const hit of hits) {
      for (let o = hit.object; o; o = o.parent) {
        if (owner.has(o)) return owner.get(o);
      }
    }
    return null;
  }

  // A click is a short press that did not move the camera.
  const canvas = office.renderer.domElement;
  let down = null;
  function onDown(e) {
    down = e.isPrimary && e.button === 0 ? { x: e.clientX, y: e.clientY } : null;
  }
  function onUp(e) {
    const d = down;
    down = null;
    if (!d || !e.isPrimary) return;
    // No time limit: on a slow frame rate a normal tap can take over a second between press and release.
    // A press that moved (camera drag) is told apart by distance and by the controls' own `dragged` flag.
    if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > CLICK_MOVE_PX) return;
    if (office.controls?.dragged) return;
    const id = pick(e.clientX, e.clientY);
    if (id != null) onSelect?.(id);
  }
  canvas.addEventListener("pointerdown", onDown);
  canvas.addEventListener("pointerup", onUp);

  function dispose() {
    canvas.removeEventListener("pointerdown", onDown);
    canvas.removeEventListener("pointerup", onUp);
    window.removeEventListener("resize", measure);
    resizeObserver?.disconnect();
    for (const tag of [...tags.values()]) removeTag(tag);
  }

  return { update, setSelected, focusLabel, pick, dispose, tags };
}
