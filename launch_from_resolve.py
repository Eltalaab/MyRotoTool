import sys
import os
import time
import json
import subprocess

DEBUG_LOG = r"e:\Apps\MyRotoTool\launch_debug.log"

def log(msg):
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass
    print(f"[AI Roto] {msg}")

def get_clip_path_comprehensive(comp):
    """Exhaustive search across all Fusion tools, Media Pool, and Timeline."""
    log("Starting clip discovery...")

    # 1. Inspect Fusion Comp Tools (MediaIn / Loader)
    tools = comp.GetToolList(False).values() if comp else []
    for tool in tools:
        reg_id = tool.GetAttrs().get("TOOLS_RegID", "")
        if reg_id not in ("MediaIn", "Loader"):
            continue

        log(f"Inspecting node: {tool.Name} ({reg_id})")

        # 1a. Check Output Metadata
        try:
            out = getattr(tool, "Output", None)
            if out:
                val = out.GetValue(comp.CurrentTime)
                if val and hasattr(val, "Metadata") and "Filename" in val.Metadata:
                    fn = val.Metadata["Filename"]
                    if fn and os.path.exists(fn):
                        log(f"Found via {tool.Name} Output.Metadata: {fn}")
                        return fn
        except Exception as e:
            log(f"Metadata check error on {tool.Name}: {e}")

        # 1b. Check TOOLST_Clip_Name for Loader
        try:
            attrs = tool.GetAttrs()
            if attrs and "TOOLST_Clip_Name" in attrs:
                names = attrs["TOOLST_Clip_Name"]
                if names and names[1]:
                    fn = comp.MapPath(names[1])
                    if fn and os.path.exists(fn):
                        log(f"Found via {tool.Name} TOOLST_Clip_Name: {fn}")
                        return fn
        except Exception as e:
            log(f"TOOLST_Clip_Name check error: {e}")

        # 1c. Check ClipID / MediaID inputs
        clip_id = None
        for in_name in ("ClipID", "MediaID", "Clip"):
            try:
                val = tool.GetInput(in_name)
                if val:
                    clip_id = str(val)
                    log(f"{tool.Name} has {in_name} = {clip_id}")
                    break
            except Exception:
                pass

    # 2. Inspect DaVinci Resolve Timeline & Media Pool
    res = globals().get("resolve")
    if not res and "bmd" in globals() and hasattr(globals()["bmd"], "scriptapp"):
        try:
            res = globals()["bmd"].scriptapp("Resolve")
        except Exception:
            pass
    if not res and "fusion" in globals() and hasattr(globals()["fusion"], "GetResolve"):
        try:
            res = globals()["fusion"].GetResolve()
        except Exception as e:
            log(f"fusion.GetResolve() error: {e}")
    if not res:
        try:
            import DaVinciResolveScript as dvr
            res = dvr.scriptapp("Resolve")
        except Exception:
            pass

    if res:
        try:
            pm = res.GetProjectManager()
            proj = pm.GetCurrentProject() if pm else None
            log(f"Current project: {proj.GetName() if proj else 'None'}")
            if proj:
                # 2a. Check Current Timeline's CurrentVideoItem
                tl = proj.GetCurrentTimeline()
                if tl:
                    log(f"Current timeline: {tl.GetName()}")
                    try:
                        item = tl.GetCurrentVideoItem()
                        if item:
                            mpi = item.GetMediaPoolItem()
                            if mpi:
                                fn = mpi.GetClipProperty("File Path")
                                if fn and os.path.exists(fn):
                                    log(f"Found via tl.GetCurrentVideoItem(): {fn}")
                                    return fn
                    except Exception as e:
                        log(f"GetCurrentVideoItem error: {e}")

                    # 2b. Check all items on Timeline video tracks
                    track_count = tl.GetTrackCount("video")
                    for tr in range(1, track_count + 1):
                        items = tl.GetItemListInTrack("video", tr) or []
                        for it in items:
                            mpi = it.GetMediaPoolItem()
                            if mpi:
                                fn = mpi.GetClipProperty("File Path")
                                if fn and os.path.exists(fn):
                                    log(f"Found via track {tr} item '{it.GetName()}': {fn}")
                                    return fn

                # 2c. Check Media Pool clips
                mp = proj.GetMediaPool()
                if mp:
                    root = mp.GetRootFolder()
                    def search_folder(folder):
                        clips = folder.GetClipList() or []
                        for c in clips:
                            fn = c.GetClipProperty("File Path")
                            if fn and os.path.exists(fn):
                                log(f"Found via Media Pool clip '{c.GetName()}': {fn}")
                                return fn
                        for sub in (folder.GetSubFolderList() or []):
                            found = search_folder(sub)
                            if found:
                                return found
                        return None

                    if root:
                        pool_clip = search_folder(root)
                        if pool_clip:
                            return pool_clip
        except Exception as e:
            log(f"Resolve API discovery error: {e}")

    log("Exhaustive search finished. No clip path resolved.")
    return ""


def main():
    log("=== launch_from_resolve.py started ===")
    fu = globals().get("fusion")
    comp = fu.GetCurrentComp() if fu else None
    log(f"Fusion: {fu}, Comp: {comp}")

    clip_path = get_clip_path_comprehensive(comp)
    log(f"FINAL RESOLVED CLIP PATH: '{clip_path}'")

    tool_dir = os.environ.get(
        "MYROTOTOOL_HOME",
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MyRotoTool"),
    )
    if clip_path:
        try:
            with open(os.path.join(tool_dir, "current_clip.txt"), "w", encoding="utf-8") as cf:
                cf.write(clip_path)
        except Exception as e:
            log(f"Error writing current_clip.txt: {e}")

    # Launch GUI
    py_exe = os.path.join(tool_dir, ".venv", "Scripts", "python.exe")
    app_py = os.path.join(tool_dir, "roto_app.py")

    cmd = [py_exe, app_py]
    if clip_path:
        cmd.append(clip_path)

    log(f"Launching subprocess: {cmd}")
    proc = subprocess.Popen(cmd, cwd=tool_dir)

    log("=== launch_from_resolve.py exited ===")

if __name__ == "__main__":
    main()
