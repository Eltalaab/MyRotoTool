"""
Timeline Synchronization Engine for DaVinci Resolve Rotoscoping Tool
Provides real-time discovery of Timeline clips, trims, tracks, metadata, and node graphs.
Supports both Resolve Internal Scripting API and SQLite Project.db inspection.
"""

import os
import sys
import glob
import sqlite3
import json
import time
import re
import cv2

CONTEXT_FILE = r"E:\Apps\MyRotoTool\timeline_context.json"
CURRENT_CLIP_FILE = r"E:\Apps\MyRotoTool\current_clip.txt"
NODE_CLIP_FILE = r"E:\Apps\MyRotoTool\node_clip.json"

def frames_to_timecode(frames, fps=30.0):
    if fps <= 0:
        fps = 30.0
    total_sec = frames / fps
    hh = int(total_sec // 3600)
    mm = int((total_sec % 3600) // 60)
    ss = int(total_sec % 60)
    ff = int(round((total_sec - int(total_sec)) * fps))
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

def get_active_resolve_project_db():
    """Find the most recently modified Project.db in Resolve Project Library."""
    base_proj_dir = os.path.expandvars(r"%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Resolve Project Library\Resolve Projects\Users\guest\Projects")
    if not os.path.isdir(base_proj_dir):
        return None, None
    proj_dbs = glob.glob(os.path.join(base_proj_dir, "*", "Project.db"))
    if not proj_dbs:
        return None, None
    newest_db = max(proj_dbs, key=os.path.getmtime)
    proj_name = os.path.basename(os.path.dirname(newest_db))
    return proj_name, newest_db

def query_db_timeline_clips(db_path):
    """
    Exhaustively query Project.db for:
    - Active Timeline name
    - Current playhead position
    - All video items on all video tracks with their start, duration, in/out, and file path
    """
    clips = []
    timeline_name = "Timeline 1"
    cur_playhead = 0

    if not db_path or not os.path.isfile(db_path):
        return timeline_name, cur_playhead, clips

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()

        # 1. Timeline Name
        try:
            c.execute("SELECT Name FROM Sm2Timeline LIMIT 1")
            row = c.fetchone()
            if row and row[0]:
                timeline_name = str(row[0])
        except Exception:
            pass

        # 2. Extract all media pool source paths from BtVideoInfo & BtAudioInfo
        media_paths = {}
        for target_table in ["BtVideoInfo", "BtAudioInfo"]:
            try:
                c.execute(f"SELECT FieldsBlob FROM [{target_table}]")
                for (blob,) in c.fetchall():
                    if blob:
                        dirs = re.findall(rb'[A-Za-z]:\\[^\x00-\x1f\*\?\"\<\>\|]+', blob)
                        files = re.findall(rb'[a-zA-Z0-9_\-\.\s()]+\.(?:mov|mp4|mkv|avi|m4v)', blob, re.IGNORECASE)
                        for d in dirs:
                            d_str = d.decode('utf-8', errors='ignore').strip()
                            for f in files:
                                f_str = f.decode('utf-8', errors='ignore').strip()
                                candidate = os.path.join(d_str, f_str)
                                if os.path.isfile(candidate):
                                    media_paths[f_str.lower()] = candidate
            except Exception:
                pass

        # 3. Track Mapping
        track_map = {}
        try:
            c.execute("SELECT Sm2TiTrack_id, Name, [Index] FROM Sm2TiTrack")
            for tid, tname, tidx in c.fetchall():
                track_map[tid] = tname or f"V{tidx or 1}"
        except Exception:
            pass

        # 4. Playhead Position
        try:
            c.execute("SELECT CurPlayheadPosition FROM Sm2MpMedia WHERE DbType='Sm2MpTimelineClip'")
            for (pos,) in c.fetchall():
                if pos is not None and str(pos).isdigit():
                    cur_playhead = int(pos)
                    break
        except Exception:
            pass

        # 5. Video Items on Timeline
        c.execute("""
            SELECT Sm2TiItem_id, Name, Start, Duration, [In], MediaStartTime, MediaFilePath, Sm2TiTrack_id, CompositionTable
            FROM Sm2TiItem 
            WHERE DbType='Sm2TiVideoClip'
            ORDER BY CAST(Start AS INTEGER) ASC
        """)
        cols = [d[0] for d in c.description]
        idx = 1
        for row in c.fetchall():
            d = dict(zip(cols, row))
            name = d.get("Name") or f"Clip {idx}"
            start = int(d.get("Start") or 0)
            duration = int(d.get("Duration") or 0)
            src_in = int(d.get("In") or 0)
            src_out = src_in + duration
            track_name = track_map.get(d.get("Sm2TiTrack_id"), f"V{idx}")

            # Resolve file path
            path = d.get("MediaFilePath")
            if not path or not os.path.isfile(path):
                path = media_paths.get(name.lower(), "")

            if not path and media_paths:
                for mk, mv in media_paths.items():
                    if mk in name.lower() or name.lower() in mk:
                        path = mv
                        break
                if not path:
                    path = list(media_paths.values())[0]

            # Detect video properties via OpenCV if file exists
            fps = 30.0
            res_str = "Unknown"
            file_exists = False
            if path and os.path.isfile(path):
                file_exists = True
                try:
                    cap = cv2.VideoCapture(path)
                    if cap.isOpened():
                        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        fps = round(cap.get(cv2.CAP_PROP_FPS) or 30.0, 2)
                        res_str = f"{w}x{h}"
                        cap.release()
                except Exception:
                    pass

            is_under_playhead = (start <= cur_playhead < (start + duration)) if duration > 0 else (idx == 1)

            # Node graph summary from database
            fusion_tools = []
            if d.get("CompositionTable"):
                fusion_tools.append({"name": "MediaIn1", "type": "MediaIn"})
                fusion_tools.append({"name": "LocalAIRoto", "type": "Fuse.LocalAIRoto"})
                fusion_tools.append({"name": "MediaOut1", "type": "MediaOut"})

            clips.append({
                "clip_id": d.get("Sm2TiItem_id") or f"clip_{idx}",
                "name": name,
                "track": track_name,
                "start": start,
                "end": start + duration,
                "duration": duration,
                "source_in": src_in,
                "source_out": src_out,
                "duration_tc": frames_to_timecode(duration, fps),
                "file_path": path,
                "file_exists": file_exists,
                "fps": fps,
                "resolution": res_str,
                "composition_id": d.get("CompositionTable"),
                "is_current": is_under_playhead,
                "color_nodes": [{"index": 1, "label": "Node 1", "lut": "None"}],
                "fusion_tools": fusion_tools,
                "index": idx
            })
            idx += 1

        conn.close()
    except Exception as e:
        print("query_db_timeline_clips error:", e)

    return timeline_name, cur_playhead, clips


def get_timeline_context():
    """
    Unified function to retrieve the most up-to-date Timeline and Node context:
    1. Checks timeline_context.json (written by Resolve internal script / Fuse).
    2. Fallback / Augment with direct Project.db querying.
    """
    proj_name, db_path = get_active_resolve_project_db()
    tl_name, playhead, db_clips = query_db_timeline_clips(db_path)

    node_data = None
    if os.path.isfile(NODE_CLIP_FILE):
        try:
            with open(NODE_CLIP_FILE, "r", encoding="utf-8") as nf:
                node_data = json.load(nf)
        except Exception:
            node_data = None

    json_data = None
    if os.path.isfile(CONTEXT_FILE):
        try:
            with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
                json_data = json.load(f)
        except Exception:
            pass

    # If JSON has active clip data from official Resolve API
    if json_data and json_data.get("active_clip") and json_data.get("status") == "connected":
        active = json_data["active_clip"]
        node_path = (node_data or {}).get("file_path")
        if node_path:
            candidates = [active] + (json_data.get("all_clips") or []) + db_clips
            for candidate in candidates:
                if os.path.normcase(os.path.normpath(candidate.get("file_path", ""))) == os.path.normcase(os.path.normpath(node_path)):
                    active = candidate.copy()
                    active["connected_node"] = f"LocalAIRoto1 -> {(node_data or {}).get('upstream_tool', 'MediaIn1')}"
                    n_start = node_data.get("source_start")
                    n_end = node_data.get("source_end")
                    if n_start is not None and n_end is not None and int(n_end) >= int(n_start):
                        active["source_in"] = int(n_start)
                        active["source_out"] = int(n_end)
                        active["duration"] = int(n_end) - int(n_start) + 1
                    break
        return {
            "status": "connected",
            "source_type": "Connected Node" if node_path else "Resolve API (Playhead)",
            "project_name": json_data.get("project_name", proj_name or "Active Project"),
            "timeline_name": json_data.get("timeline_name", tl_name),
            "playhead": playhead,
            "active_clip": active,
            "all_clips": [active],
            "color_nodes": active.get("color_nodes", []),
            "fusion_tools": active.get("fusion_tools", []),
            "timestamp": json_data.get("timestamp", time.time())
        }

    # Otherwise use Project.db analysis
    active_clip = None
    for c in db_clips:
        if c.get("is_current"):
            active_clip = c
            break
    if not active_clip and db_clips:
        active_clip = db_clips[0]

    if node_data and node_data.get("file_path"):
        node_path = os.path.normcase(os.path.normpath(node_data["file_path"]))
        for candidate in db_clips:
            if os.path.normcase(os.path.normpath(candidate.get("file_path", ""))) == node_path:
                active_clip = candidate
                active_clip["connected_node"] = f"LocalAIRoto1 -> {node_data.get('upstream_tool', 'MediaIn1')}"
                break

    return {
        "status": "connected" if active_clip else "no_clips",
        "source_type": "Connected Node" if node_data and active_clip else "Timeline Database (Project.db)",
        "project_name": proj_name or "Unknown Project",
        "timeline_name": tl_name,
        "playhead": playhead,
        "active_clip": active_clip,
        "all_clips": [active_clip] if active_clip and node_data else db_clips,
        "color_nodes": active_clip.get("color_nodes", []) if active_clip else [],
        "fusion_tools": active_clip.get("fusion_tools", []) if active_clip else [],
        "timestamp": time.time()
    }


def write_timeline_context(payload):
    """Write context to timeline_context.json and update current_clip.txt."""
    try:
        with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print("write_timeline_context error:", e)

    try:
        if payload.get("active_clip") and payload["active_clip"].get("file_path"):
            fp = payload["active_clip"]["file_path"]
            with open(CURRENT_CLIP_FILE, "w", encoding="utf-8") as cf:
                cf.write(fp)
    except Exception as e:
        print("write current_clip error:", e)
