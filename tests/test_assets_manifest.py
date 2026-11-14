"""Checks that the 3D asset manifest is consistent with the files and CREDITS.md.

Validates: Requirements 2.2, 2.11
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "skeleton" / "office" / "static" / "assets"
MANIFEST = ASSETS / "manifest.json"
CREDITS = ROOT / "CREDITS.md"
BEHAVIORS = {"idle", "walk", "sit_down", "sit_idle", "type", "talk", "wave", "stretch", "drink"}
ASSET_KINDS = ("characters", "animations", "furniture")


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _listed_files(manifest):
    for key in ("characters", "hair", "animations", "furniture"):
        for item in manifest[key]:
            yield item["file"]
    yield from manifest["furniture_roles"].values()


def test_manifest_is_valid_json_with_required_sections(manifest):
    for key in ("rigs", "characters", "animations", "clip_map", "furniture"):
        assert manifest[key], key
    assert len(manifest["characters"]) >= 4


def test_every_referenced_file_exists(manifest):
    missing = [f for f in _listed_files(manifest) if not (ASSETS / f).is_file()]
    assert not missing, missing


def test_gltf_external_uris_exist(manifest):
    """A .gltf must find its .bin and textures, or the browser load fails."""
    for item in manifest["characters"] + manifest["hair"]:
        path = ASSETS / item["file"]
        data = json.loads(path.read_text(encoding="utf-8"))
        uris = [b["uri"] for b in data.get("buffers", []) if "uri" in b]
        uris += [i["uri"] for i in data.get("images", []) if "uri" in i]
        for uri in uris:
            if uri.startswith("data:"):
                continue
            assert (path.parent / uri).is_file(), f"{item['file']} -> {uri}"


def test_clip_map_covers_every_behavior(manifest):
    assert set(manifest["clip_map"]) == BEHAVIORS


def test_non_null_clip_references_exist_in_listing(manifest):
    clips_by_file = {a["file"]: set(a["clips"]) for a in manifest["animations"]}
    refs = [v for v in manifest["clip_map"].values() if v is not None]
    refs += list(manifest["extra_clips"].values())
    assert refs, "clip_map has no usable clips"
    for ref in refs:
        assert ref["file"] in clips_by_file, ref
        assert ref["clip"] in clips_by_file[ref["file"]], ref


def test_null_behaviors_are_documented(manifest):
    for behavior, ref in manifest["clip_map"].items():
        if ref is None:
            assert behavior in manifest["missing_behaviors"], behavior


def test_characters_and_animations_share_one_rig(manifest):
    rig_ids = {x["rig"] for x in manifest["characters"] + manifest["hair"] + manifest["animations"]}
    assert rig_ids == {"quaternius_humanoid_65"}
    rig = manifest["rigs"]["quaternius_humanoid_65"]
    assert rig["joint_count"] == len(rig["joints"]) == len(set(rig["joints"]))


def test_characters_have_distinct_looks(manifest):
    looks = {(c["body"], c["skin_texture"]) for c in manifest["characters"]}
    assert len(looks) == len(manifest["characters"])


def test_credits_mentions_each_asset_pack_folder():
    credits = CREDITS.read_text(encoding="utf-8")
    pack_dirs = [p for kind in ASSET_KINDS for p in sorted((ASSETS / kind).iterdir()) if p.is_dir()]
    assert pack_dirs
    for pack in pack_dirs:
        assert pack.name in credits, f"CREDITS.md does not mention {pack.name}"


def test_credits_lists_license_and_three_js():
    credits = CREDITS.read_text(encoding="utf-8")
    assert "CC0 1.0" in credits
    assert "three.js" in credits and "MIT" in credits


def test_every_pack_folder_ships_its_license_file():
    for kind in ASSET_KINDS:
        for pack in (ASSETS / kind).iterdir():
            if pack.is_dir():
                assert list(pack.glob("License*.txt")), f"no license file in {pack}"
