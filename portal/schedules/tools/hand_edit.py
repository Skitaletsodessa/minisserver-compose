#!/usr/bin/env python3
"""Text-level surgery on the hand-written automations.yaml / scripts.yaml for one entity's cutover (Task 27 Phase D).

The hand-written files keep their comments and layout: only the blocks that belong to the migrated entity are removed. Files are
rewritten IN PLACE (same inode): automations.yaml/scripts.yaml are single-file bind mounts into the HA container and a replaced file
would leave the container looking at the old content (CLAUDE.md, the 'docker inspect lies' note).

  python tools/hand_edit.py ENTITY [--dir DIR] [--dry-run]       # remove the entity's hand-written schedule items
  python tools/hand_edit.py --init [--dir DIR]                   # one-off: reconcile_all_plugs also calls script.sch_reconcile_all,
                                                                 #           add the static webhook automation + (nothing else)
"""
import os
import re
import sys
import argparse
import difflib

# entity id (portal) -> what to take out of the hand-written files
PLAN = {
    "bathroom_towels": {"ha": "switch.bathroom_towels",
                        "automations": ["sched_bathroom_towels_on_morning", "sched_bathroom_towels_off_morning", "sched_bathroom_towels_on_evening",
                                        "sched_bathroom_towels_off_evening", "reconcile_bathroom_towels_reconnect"],
                        "scripts": ["reconcile_bathroom_towels"]},
    "plant_rack1": {"ha": "switch.living_room_rack", "automations": ["reconcile_rack1_reconnect"], "lists": True},
    "plant_rack2": {"ha": "switch.livingroom_rack_big", "automations": ["reconcile_rack2_reconnect"], "lists": True},
    "plant_orchids": {"ha": "switch.kitchen_orchids", "automations": ["reconcile_orchids_reconnect"], "lists": True},
    "plant_cacti": {"ha": "switch.minisserver", "automations": ["reconcile_minisserver_reconnect"], "lists": True},
    "parrot_uv": {"ha": "switch.bedroom_parrot", "automations": ["sched_parrot_uv_on", "sched_parrot_uv_off", "reconcile_parrot_reconnect"],
                  "scripts": ["reconcile_parrot"]},
    "vacuum_livingroom": {"ha": "script.vacuum_livingroom_plan", "automations": ["sched_vacuum_livingroom_plan"]},
    "vacuum_bedroom": {"ha": "script.vacuum_bedroom_plan", "automations": ["sched_vacuum_bedroom_morning", "sched_vacuum_bedroom_evening"]},
}
PLANT_BLOCKS = ["sched_plant_lights_on", "sched_plant_lights_off"]
WEBHOOK_AUTOMATION = """
# ── Portal (Task 27): the one way the host-side applier reaches Home Assistant. Static and hand-written on purpose: it contains no time and no
# schedule, only "reload what is in generated/". local_only: only requests from this machine's own network are accepted; the webhook id is a
# secret (secrets.yaml, not in git). script.reload first, automation.reload last: the automation reload replaces this very automation.
- id: portal_apply_reload
  alias: 'Portal: reload generated schedules (webhook)'
  mode: single
  trigger:
    - platform: webhook
      webhook_id: !secret portal_reload_webhook
      allowed_methods: [POST]
      local_only: true
  action:
    - service: script.reload
    - service: automation.reload
"""


def block_range(lines, start_pred, stop_pred):
    """Index range [i, j) of a block starting at the first line matching start_pred, ending before the next line matching stop_pred."""
    for i, l in enumerate(lines):
        if start_pred(l):
            j = i + 1
            while j < len(lines) and not stop_pred(lines[j]):
                j += 1
            return i, j
    return None


def remove_automation(text, aid):
    lines = text.split("\n")
    r = block_range(lines, lambda l: l.strip() == "- id: " + aid and l.startswith("- "), lambda l: l.startswith("- id:") or l.startswith("#"))
    if not r:
        raise KeyError("automation %s not found" % aid)
    i, j = r
    while j < len(lines) and lines[j].strip() == "" and j + 1 < len(lines) and lines[j + 1].strip() == "":
        j += 1
    del lines[i:j]
    return "\n".join(lines)


def remove_script(text, key):
    lines = text.split("\n")
    r = block_range(lines, lambda l: l.startswith(key + ":"), lambda l: l and not l.startswith((" ", "\t")))
    if not r:
        raise KeyError("script %s not found" % key)
    i, j = r
    del lines[i:j]
    return "\n".join(lines)


def drop_script_call(text, key):
    out = [l for l in text.split("\n") if l.strip() != "- service: script." + key]
    return "\n".join(out)


def drop_entity_from_block(text, block_id, entity, container="automation"):
    """Remove `- switch.x` list items inside one automation block; returns (text, remaining entity count in that block)."""
    lines = text.split("\n")
    if container == "automation":
        r = block_range(lines, lambda l: l.strip() == "- id: " + block_id and l.startswith("- "), lambda l: l.startswith("- id:") or l.startswith("#"))
    else:
        r = block_range(lines, lambda l: l.startswith(block_id + ":"), lambda l: l and not l.startswith((" ", "\t")))
    if not r:
        raise KeyError("block %s not found" % block_id)
    i, j = r
    keep = [l for k, l in enumerate(lines[i:j]) if l.strip() != "- " + entity]
    lines[i:j] = keep
    remaining = sum(1 for l in keep if re.match(r"^\s*- switch\.[a-z0-9_]+\s*$", l))
    return "\n".join(lines), remaining


def edit(entity, auto_text, script_text):
    plan = PLAN[entity]
    for aid in plan.get("automations", []):
        auto_text = remove_automation(auto_text, aid)
    for key in plan.get("scripts", []):
        script_text = remove_script(script_text, key)
        script_text = drop_script_call(script_text, key)
    if plan.get("lists"):
        gone = []
        for b in PLANT_BLOCKS:
            auto_text, rem = drop_entity_from_block(auto_text, b, plan["ha"])
            if rem == 0:
                auto_text = remove_automation(auto_text, b)
                gone.append(b)
        script_text, rem = drop_entity_from_block(script_text, "reconcile_plant_lights", plan["ha"], container="script")
        if rem == 0:
            script_text = remove_script(script_text, "reconcile_plant_lights")
            script_text = drop_script_call(script_text, "reconcile_plant_lights")
    return auto_text, script_text


def init(auto_text, script_text):
    if "portal_apply_reload" in auto_text:
        raise ValueError("already initialised")
    auto_text = auto_text.rstrip("\n") + "\n" + WEBHOOK_AUTOMATION
    anchor = "    - service: script.reconcile_parrot"
    if anchor not in script_text:
        raise KeyError("reconcile_all_plugs layout changed: " + anchor)
    script_text = script_text.replace(anchor, anchor + "\n    - service: script.sch_reconcile_all", 1)
    return auto_text, script_text


def write_in_place(path, text):
    with open(path, "r+", encoding="utf-8") as f:       # same inode: the container's single-file bind mount keeps seeing it
        f.seek(0)
        f.write(text)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entity", nargs="?")
    ap.add_argument("--dir", default="/srv/compose/homeassistant")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ap_, sp_ = os.path.join(a.dir, "automations.yaml"), os.path.join(a.dir, "scripts.yaml")
    at, st = open(ap_, encoding="utf-8").read(), open(sp_, encoding="utf-8").read()
    if a.init:
        na, ns = init(at, st)
    elif a.entity in PLAN:
        na, ns = edit(a.entity, at, st)
    else:
        sys.exit("usage: hand_edit.py ENTITY|--init  (entities: %s)" % ", ".join(PLAN))
    for name, old, new in (("automations.yaml", at, na), ("scripts.yaml", st, ns)):
        d = list(difflib.unified_diff(old.split("\n"), new.split("\n"), name, name, lineterm="", n=1))
        print("\n".join(d) if d else "(%s unchanged)" % name)
    if not a.dry_run:
        write_in_place(ap_, na)
        write_in_place(sp_, ns)
        print("written in place")


if __name__ == "__main__":
    main()
