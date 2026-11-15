"""Structure checks for the character module (task 8.3).

String checks only. They need no Node, no browser, and no network.
"""
import json
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "skeleton" / "office" / "static"
MANIFEST = json.loads((STATIC / "assets" / "manifest.json").read_text(encoding="utf-8"))


def read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def js_string_array(js: str, name: str) -> list[str]:
    """Strings of `export const NAME = Object.freeze([...])` (or a plain array)."""
    match = re.search(rf"\b{name}\s*=\s*Object\.freeze\(\s*\[(.*?)\]\s*\)", js, re.S)
    assert match, f"{name} not found"
    return re.findall(r'"([^"]+)"', match.group(1))


@pytest.mark.parametrize("rel", ["characters.js", "char-look.js", "dev/characters.html"])
def test_files_exist(rel):
    path = STATIC / rel
    assert path.is_file(), f"missing {rel}"
    assert path.stat().st_size > 0


def test_public_exports_are_present():
    js = read("characters.js")
    assert re.search(r"export\s+async\s+function\s+loadAssets\s*\(\s*manifest\s*,\s*onProgress", js)
    assert re.search(r"export\s+function\s+createCharacter\s*\(\s*assets\s*,\s*agentId", js)
    assert re.search(r"export\s+function\s+lookForAgent\s*\(", js)
    assert re.search(r"export\s+function\s+seatedRoot\s*\(", js)
    for const in ("CROSSFADE", "WALK_NATURAL_SPEED", "WALK_SPEED", "BEHAVIORS"):
        assert re.search(rf"export\s+const\s+{const}\b", js), const


def test_character_api_surface():
    js = read("characters.js")
    for name in ("object3D", "mixer", "play", "setBehavior", "lookAt", "bones", "update", "dispose"):
        assert re.search(rf"\b{name}\b", js), name
    # one AnimationMixer per character, created inside createCharacter
    assert js.count("new THREE.AnimationMixer(model)") == 1
    # cross-fade through crossFadeTo, 0.3 s (Req 2.3)
    assert "crossFadeTo(" in js
    assert re.search(r"export\s+const\s+CROSSFADE\s*=\s*0\.3\b", js)


def test_no_external_urls_or_cdn_imports():
    for rel in ("characters.js", "char-look.js", "dev/characters.html"):
        text = read(rel)
        assert not re.search(r"https?://", text), f"external URL in {rel}"
        assert not re.search(r"""from\s+["']https?:""", text), rel
    html = read("dev/characters.html")
    for attr in re.findall(r'\b(?:src|href)\s*=\s*"([^"]*)"', html):
        assert not re.match(r"(?i)(https?:)?//", attr), attr


def test_imports_use_the_vendored_three():
    js = read("characters.js")
    assert 'from "three"' in js
    assert 'from "three/addons/loaders/GLTFLoader.js"' in js
    assert 'from "three/addons/utils/SkeletonUtils.js"' in js
    html = read("dev/characters.html")
    match = re.search(r'<script\s+type="importmap"\s*>(.*?)</script>', html, re.S)
    assert match, "dev page needs an importmap"
    assert '"three": "../vendor/three.module.js"' in match.group(1)
    assert '"three/addons/": "../vendor/addons/"' in match.group(1)
    for rel in ("vendor/three.module.js", "vendor/addons/loaders/GLTFLoader.js", "vendor/addons/utils/SkeletonUtils.js"):
        assert (STATIC / rel).is_file(), rel


def test_dev_page_wiring():
    html = read("dev/characters.html")
    assert re.search(r'from\s+"\.\./office\.js"', html)
    assert re.search(r'from\s+"\.\./characters\.js"', html)
    assert "deskForAgent" in html
    assert "maxAgents" in html  # up to 8 characters
    assert "../assets/manifest.json" in html
    for behavior in ("idle", "walk", "sit", "type", "talk", "wave", "stretch", "drink"):
        assert f'["{behavior}",' in html, behavior


def test_every_clip_key_used_exists_in_the_manifest():
    js = read("characters.js")
    clip_map = MANIFEST["clip_map"]
    extra = MANIFEST["extra_clips"]
    required = js_string_array(js, "REQUIRED_CLIPS")
    optional = js_string_array(js, "OPTIONAL_CLIPS")
    for key in required:
        assert clip_map.get(key), f"required clip key {key!r} is not mapped in clip_map"
    for key in optional:
        assert (clip_map.get(key) or extra.get(key)), f"optional clip key {key!r} is not in the manifest"
    # every literal key passed to play(...) or play(cond ? "a" : "b") is a known clip key
    known = set(required) | set(optional)
    used = set(re.findall(r'\bplay\(\s*"([a-z_]+)"', js))
    used |= set(re.findall(r'\bplay\(state\.seated \? "([a-z_]+)" : "([a-z_]+)"', js)[0]) if re.search(
        r'\bplay\(state\.seated \? "([a-z_]+)" : "([a-z_]+)"', js) else set()
    used |= set(re.findall(r'ensureStanding\(\s*"([a-z_]+)"', js))
    assert used, "no clip keys found"
    assert used <= known, f"unknown clip keys: {sorted(used - known)}"
    # the mapped clip names exist in the animation files listed in the manifest
    clips_by_file = {a["file"]: set(a["clips"]) for a in MANIFEST["animations"]}
    for key in sorted(known):
        spec = clip_map.get(key) or extra.get(key)
        assert spec["file"] in clips_by_file, spec
        assert spec["clip"] in clips_by_file[spec["file"]], f"{key}: clip {spec['clip']} missing in {spec['file']}"
        assert (STATIC / "assets" / spec["file"]).is_file(), spec["file"]


def test_procedural_behaviors_have_no_clip_and_a_layer():
    js = read("characters.js")
    for name in ("type", "wave", "stretch", "drink"):
        assert MANIFEST["clip_map"][name] is None, f"{name} is expected to be procedural"
        assert re.search(rf"\b{name}:\s*\{{\s*w:\s*0", js), f"no procedural layer for {name}"
    assert "raise_hand" in js


def test_idle_behaviors_never_use_the_typing_layer():
    js = read("characters.js")
    assert re.search(r'layers\.type\.goal\s*=\s*state\.behavior\s*===\s*"type"', js)
    # type is only requested for the "type" behavior; no other case switches that layer on
    assert len(re.findall(r"layers\.type\.goal\s*=\s*(?!0)", js)) == 1
    assert "LAYER_RAMP = 0.3" in js


def test_walk_speed_constants_match_the_manifest_hints():
    js = read("characters.js")
    hints = MANIFEST["playback_hints"]
    assert f"WALK_NATURAL_SPEED = {hints['walk_natural_speed_mps']}" in js
    assert "WALK_SPEED = 1.2" in js
    assert f"SEAT_PELVIS_HEIGHT = {hints['sit_pelvis_height_m']}" in js
    assert f"SEAT_PELVIS_BACK = {hints['sit_pelvis_back_offset_m']}" in js


def test_looks_use_the_loaded_catalogue_and_a_fixed_palette():
    js = read("characters.js")
    look = read("char-look.js")
    hair_ids = {h["id"] for h in MANIFEST["hair"]}
    for hair in set(re.findall(r'"(hair_[a-z]+)"', js + look)):
        assert hair in hair_ids, hair
    body_ids = {c["id"] for c in MANIFEST["characters"]}
    for body in ("male", "female"):
        for skin in ("dark", "light"):
            assert f"superhero_{body}_{skin}" in body_ids
    assert "`superhero_${gender}_${tone.variant}`" in look
    for palette in ("SHIRT_COLORS", "PANTS_COLORS", "SHOE_COLORS", "HAIR_COLORS", "SKIN_TONES"):
        assert re.search(rf"export\s+const\s+{palette}\b", look), palette
        assert re.search(rf"\b{palette}\b", js.split("// ---- constants")[0] + js.split("export {")[1].split("}")[0]), palette
    # seeded by agent id only, never by Math.random (same look after every reload, Req 2.2)
    assert "Math.random" not in js and "Math.random" not in look
    assert "hash32" in look and "mulberry32" in look
    assert 'from "./char-look.js"' in js


def test_char_look_is_pure_and_shared_with_the_page():
    look = read("char-look.js")
    # no Three.js and no DOM in the module, so Node can test it (tests/js/char-look.test.mjs)
    assert not re.search(r"^\s*import\b", look, re.M)
    assert not re.search(r"\b(document|window|THREE)\b", look)
    for name in ("resolveGender", "genderFromName", "genderFromId", "traitsForAgent"):
        assert re.search(rf"export\s+function\s+{name}\s*\(", look), name
    for name in ("FEMALE_NAMES", "MALE_NAMES", "BUILD_RANGES"):
        assert re.search(rf"export\s+const\s+{name}\b", look), name
    # characters.js takes `gender`, `name` and `look` options and passes them to the look
    js = read("characters.js")
    assert re.search(r"traitsForAgent\(agentId,\s*\{\s*\.\.\.overrides", js)
    assert re.search(r"^\s*\*\s+gender, name\b", js, re.M)
    assert re.search(r"^\s*\*\s+look\b", js, re.M)


def test_main_passes_the_agent_name_to_the_character():
    main = read("main.js")
    assert re.search(r"createCharacter\(assets,\s*id,\s*\{[^}]*name:\s*agent\.name", main)
    # the mock agents have names the look can use
    for name in ("Sari", "Budi", "Wati", "Joko"):
        assert f'name: "{name}"' in main
    dev = read("dev/characters.html")
    assert "name: names[i]" in dev


def test_body_proportions_are_bone_scales_with_ground_contact():
    js = read("characters.js")
    # height on the model, width and depth on spine_01 (inverse on neck_01), arms on upperarm
    assert re.search(r"function\s+applyBuild\(", js)
    assert "model.scale.setScalar(build.height)" in js
    assert "bones.spine_01.scale.set(build.width, 1, build.depth)" in js
    assert "bones.neck_01.scale.set(1 / build.width, 1, 1 / build.depth)" in js
    assert "upperarm_${side}`].scale.set(build.arm" in js
    # the soles follow the height, a seated shorter character keeps the pelvis near the cushion
    assert "ground.idle * hs" in js and "seatComp" in js
    # shirt patterns and the headscarf are procedural, no new asset files
    assert "uPattern" in js and "makeHijab" in js
    assert not re.search(r"https?://", js)


def test_shared_resources_are_not_mutated():
    js = read("characters.js")
    # materials are cloned per agent before tinting
    assert js.count("base.clone()") >= 2
    assert "SkeletonUtils.clone(src.scene)" in js
    # hair is re-bound to the character's own skeleton, geometry shared with the loaded file
    assert "new THREE.SkinnedMesh(h.mesh.geometry" in js
    assert "mesh.bind(skeleton" in js
    # only the pelvis position track is kept from the clips (other bone offsets would stretch the body)
    assert 't.name === "pelvis.position"' in js
