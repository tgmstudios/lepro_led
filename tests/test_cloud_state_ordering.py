"""Cloud-scene identity, report/catalog ordering, stale-state and pending-confirmation semantics (synthetic data)."""
import asyncio
import json

import pytest

from custom_components.lepro_led import protocol as p
from custom_components.lepro_led.api import CloudScene
from custom_components.lepro_led.runtime import LeproRuntime
from custom_components.lepro_led.state import DeviceState

from .test_runtime import FakeTransport, GatedTransport, _spin, msg

LOCAL = p.build_d50([(9, 8, 7)] * 25, "gradient", 40)
# a cloud string whose head parses as the six known codec (palette/effect), but is a server-defined scene
PARTIAL = LOCAL[:-1] + "ZZSRV;"
OPAQUE = "N01:P1000212345678ABCDEFF2100021234ABCDU3VFAKE0001;"
SC_P = CloudScene("cid-1", "Partial", PARTIAL)
SC_O = CloudScene("cid-2", "Opaque", OPAQUE)


def blank(st):
    return st.effect is None and st.direction is None and st.palette == [] and st.rgb is None and st.cloud_scene is None


def test_report_before_catalog_then_catalog_shows_cloud_scene_without_local_attrs():
    s = DeviceState()
    s.apply_report({"d2": 2, "d50": PARTIAL})
    assert parse_ok(PARTIAL)  # generic parse alone would claim a local gradient
    assert s.effect == "gradient"
    s.cloud_scenes = {PARTIAL: "Partial"}
    s.reevaluate_cloud()
    assert s.cloud_scene == "Partial" and s.effect is None and s.direction is None and s.palette == [] and s.rgb is None


def parse_ok(d):
    return p.parse_d50(d) is not None


def test_known_cloud_scene_identity_beats_partial_codec_parse():
    s = DeviceState()
    s.cloud_scenes = {PARTIAL: "Partial"}
    s.apply_report({"d2": 2, "d50": PARTIAL})
    assert s.cloud_scene == "Partial" and s.effect is None and s.palette == [] and s.rgb is None


def test_removed_active_scene_does_not_fall_back_to_stale_local_values():
    s = DeviceState()
    s.apply_report({"d2": 2, "d50": LOCAL})  # earlier local truth
    s.cloud_scenes = {OPAQUE: "Opaque"}
    s.apply_report({"d2": 2, "d50": OPAQUE})
    s.cloud_scenes = {}
    s.reevaluate_cloud()  # catalog refresh removed the active scene
    assert s.d50_str == OPAQUE and blank(s)


def test_removed_active_scene_reparsed_only_when_exact_codec():
    s = DeviceState()
    s.cloud_scenes = {LOCAL: "Same as local"}
    s.apply_report({"d2": 2, "d50": LOCAL})
    assert s.cloud_scene == "Same as local"
    s.cloud_scenes = {}
    s.reevaluate_cloud()  # exact round-trip -> proven local codec
    assert s.cloud_scene is None and s.effect == "gradient" and s.palette == [(9, 8, 7)] and s.rgb == (9, 8, 7)
    # partial-codec raw string is NOT reinterpreted
    s2 = DeviceState()
    s2.cloud_scenes = {PARTIAL: "P"}
    s2.apply_report({"d2": 2, "d50": PARTIAL})
    s2.cloud_scenes = {}
    s2.reevaluate_cloud()
    assert blank(s2)


def test_new_local_d50_report_or_command_clears_raw_cloud_state():
    s = DeviceState()
    s.cloud_scenes = {OPAQUE: "O"}
    s.apply_report({"d2": 2, "d50": OPAQUE})
    s.apply_report({"d2": 2, "d50": LOCAL})
    assert s.cloud_scene is None and s.effect == "gradient" and s.rgb == (9, 8, 7)
    s.apply_report({"d2": 2, "d50": OPAQUE})
    s.command_turn_on(rgb=(1, 2, 3))
    assert s.effect is not None and s.rgb == (1, 2, 3)


# ---- runtime ordering / pending ----------------------------------------------------
def rt_with(*scenes):
    rt = LeproRuntime([{"did": 111, "fid": 1}])
    rt.transport = FakeTransport()
    if scenes:
        rt.set_scenes("111", list(scenes))
    return rt


def test_runtime_seed_and_report_before_set_scenes_then_catalog_arrives():
    rt = LeproRuntime([{"did": 111, "fid": 1, "d50": PARTIAL, "d2": 2}])
    st = rt.devices["111"].state
    assert st.effect == "gradient"
    rt.set_scenes("111", [SC_P])
    assert st.cloud_scene == "Partial" and st.effect is None and st.palette == [] and st.rgb is None


def test_runtime_catalog_refresh_removing_active_scene_stays_blank():
    rt = rt_with(SC_O)
    msg(rt, 111, "rpt", {"d2": 2, "d50": OPAQUE})
    rt.set_scenes("111", [SC_P])
    assert blank(rt.devices["111"].state)


async def _select(rt, scene):
    await rt.send("111", lambda s: s.command_cloud_scene(scene.d50))


async def test_unrelated_d52_report_leaves_raw_pending():
    rt = rt_with(SC_O, SC_P)
    await _select(rt, SC_O)
    assert "d50_str" in rt.pending_confirmation("111")
    msg(rt, 111, "rpt", {"d52": 500})
    assert "d50_str" in rt.pending_confirmation("111")


async def test_different_d50_report_clears_claim_and_shows_device_truth():
    rt = rt_with(SC_O, SC_P)
    await _select(rt, SC_O)
    msg(rt, 111, "rpt", {"d2": 2, "d50": PARTIAL})  # device is actually somewhere else
    st = rt.devices["111"].state
    assert "d50_str" not in rt.pending_confirmation("111")  # claim dropped, not left dangling
    assert st.d50_str == PARTIAL and st.cloud_scene == "Partial"  # truth shown; not a false confirmation of Opaque


async def test_cached_matching_raw_does_not_prematurely_confirm_command():
    rt = rt_with(SC_O)
    msg(rt, 111, "rpt", {"d2": 2, "d50": OPAQUE})  # already in that scene before we publish
    assert "d50_str" not in rt.pending_confirmation("111")
    await _select(rt, SC_O)
    assert "d50_str" in rt.pending_confirmation("111")  # waits for a post-publish report
    msg(rt, 111, "rpt", {"d2": 2, "d50": OPAQUE})
    assert "d50_str" not in rt.pending_confirmation("111")
    assert rt.devices["111"].state.cloud_scene == "Opaque"


async def test_stale_report_during_publish_does_not_confirm_newer_desired():
    rt = LeproRuntime([{"did": 111, "fid": 1}])
    rt.transport = t = GatedTransport()
    rt.set_scenes("111", [SC_O, SC_P])
    job = asyncio.create_task(_select(rt, SC_P))
    await _spin()
    msg(rt, 111, "rpt", {"d2": 2, "d50": OPAQUE})  # stale report (older command) lands mid-publish
    t.gates[0].set()
    await job
    st = rt.devices["111"].state
    assert st.d50_str == OPAQUE and st.cloud_scene == "Opaque"  # device truth, newer desired not committed
    assert "d50_str" not in rt.pending_confirmation("111")
    msg(rt, 111, "rpt", {"d2": 2, "d50": PARTIAL})
    assert st.cloud_scene == "Partial"
