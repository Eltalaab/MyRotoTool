"""
AI Rotoscope Integration Script for DaVinci Resolve.
Executed from DaVinci Resolve internal menu:
Workspace -> Scripts -> Edit (or Comp/Color/Utility) -> AI_Rotoscope.py
Or triggered via Fusion Fuse.
"""

import sys
import os
import json
import time
import subprocess

CONTEXT_FILE = r"E:\Apps\MyRotoTool\timeline_context.json"
CURRENT_CLIP_FILE = r"E:\Apps\MyRotoTool\current_clip.txt"
ROTO_BAT = r"E:\Apps\MyRotoTool\run_roto.bat"

def get_resolve_objects():
    res = globals().get("resolve")
    fu = globals().get("fusion")

    if not res and fu and hasattr(fu, "GetResolve"):
        try:
            res = fu.GetResolve()
        except Exception:
            pass

    if not res and "app" in globals() and hasattr(globals()["app"], "GetResolve"):
        try:
            res = globals()["app"].GetResolve()
        except Exception:
            pass

    if not res and "bmd" in globals() and hasattr(globals()["bmd"], "scriptapp"):
        try:
            res = globals()["bmd"].scriptapp("Resolve")
        except Exception:
            pass

    return res, fu

def sync_active_timeline_item():
    res, fu = get_resolve_objects()
    if not res:
        print("[AI Rotoscope] Could not get Resolve API object.")
        return None

    pm = res.GetProjectManager()
    proj = pm.GetCurrentProject() if pm else None
    if not proj:
        print("[AI Rotoscope] No active project open in Resolve.")
        return None

    tl = proj.GetCurrentTimeline()
    if not tl:
        print("[AI Rotoscope] No active timeline found in project.")
        return None

    current_item = tl.GetCurrentVideoItem()
    current_tc = tl.GetCurrentTimecode() if hasattr(tl, "GetCurrentTimecode") else ""

    # All video clips on timeline
    all_clips = []
    track_count = tl.GetTrackCount("video") or 1
    for tr in range(1, track_count + 1):
        items = tl.GetItemListInTrack("video", tr) or []
        for it in items:
            try:
                mpi = it.GetMediaPoolItem()
                fp = mpi.GetClipProperty("File Path") if mpi else ""
                s_in = it.GetSourceStartFrame() if hasattr(it, "GetSourceStartFrame") else 0
                s_out = it.GetSourceEndFrame() if hasattr(it, "GetSourceEndFrame") else it.GetDuration()
                all_clips.append({
                    "name": it.GetName(),
                    "track": f"V{tr}",
                    "start": it.GetStart(),
                    "end": it.GetEnd(),
                    "duration": it.GetDuration(),
                    "source_in": s_in,
                    "source_out": s_out,
                    "file_path": fp,
                    "fps": mpi.GetClipProperty("FPS") if mpi else tl.GetSetting("timelineFrameRate"),
                    "resolution": mpi.GetClipProperty("Resolution") if mpi else f"{tl.GetSetting('timelineResolutionWidth')}x{tl.GetSetting('timelineResolutionHeight')}"
                })
            except Exception as e:
                print("[AI Rotoscope] Error reading track item:", e)

    # Active Clip Details
    active_clip_data = None
    color_nodes = []
    fusion_tools = []

    if current_item:
        mpi = current_item.GetMediaPoolItem()
        fp = mpi.GetClipProperty("File Path") if mpi else ""
        s_in = current_item.GetSourceStartFrame() if hasattr(current_item, "GetSourceStartFrame") else 0
        s_out = current_item.GetSourceEndFrame() if hasattr(current_item, "GetSourceEndFrame") else current_item.GetDuration()
        
        # Color Nodes
        try:
            ng = current_item.GetNodeGraph()
            if ng:
                n_count = ng.GetNumNodes() or 0
                for n_idx in range(1, n_count + 1):
                    lbl = ng.GetNodeLabel(n_idx) or f"Node {n_idx}"
                    lut = ng.GetLUT(n_idx) or "None"
                    color_nodes.append({"index": n_idx, "label": lbl, "lut": lut})
        except Exception as e:
            pass

        # Fusion Tools
        try:
            comp_cnt = current_item.GetFusionCompCount() if hasattr(current_item, "GetFusionCompCount") else 0
            if comp_cnt > 0:
                f_comp = current_item.GetFusionCompByIndex(1)
                if f_comp:
                    for t_name, t_obj in f_comp.GetToolList(False).items():
                        reg = t_obj.GetAttrs().get("TOOLS_RegID", "")
                        fusion_tools.append({"name": t_name, "type": reg})
        except Exception:
            pass

        active_clip_data = {
            "name": current_item.GetName(),
            "file_path": fp,
            "start": current_item.GetStart(),
            "end": current_item.GetEnd(),
            "duration": current_item.GetDuration(),
            "source_in": s_in,
            "source_out": s_out,
            "fps": mpi.GetClipProperty("FPS") if mpi else tl.GetSetting("timelineFrameRate"),
            "resolution": mpi.GetClipProperty("Resolution") if mpi else "",
            "color_nodes": color_nodes,
            "fusion_tools": fusion_tools
        }

    payload = {
        "status": "connected",
        "source_type": "Resolve API (Playhead)",
        "timestamp": time.time(),
        "project_name": proj.GetName(),
        "timeline_name": tl.GetName(),
        "current_timecode": current_tc,
        "active_clip": active_clip_data,
        "all_clips": all_clips,
        "color_nodes": color_nodes,
        "fusion_tools": fusion_tools
    }

    try:
        with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"[AI Rotoscope] Successfully exported timeline context: {active_clip_data.get('name') if active_clip_data else 'None'}")
    except Exception as e:
        print("[AI Rotoscope] Could not write context file:", e)

    if active_clip_data and active_clip_data.get("file_path"):
        try:
            with open(CURRENT_CLIP_FILE, "w", encoding="utf-8") as cf:
                cf.write(active_clip_data["file_path"])
        except Exception:
            pass

    return payload

def main():
    ctx = sync_active_timeline_item()
    
    # Check if roto_app is already running
    # If not running, launch it
    try:
        output = subprocess.check_output('tasklist /FI "IMAGENAME eq python.exe"', shell=True).decode()
        if "python.exe" not in output:
            subprocess.Popen([ROTO_BAT], shell=True)
            print("[AI Rotoscope] Launched Rotoscoping App.")
        else:
            print("[AI Rotoscope] Rotoscoping App is already open. Context refreshed.")
    except Exception:
        pass

if __name__ == "__main__":
    main()
