import os
import glob
import sqlite3
import re
import sys

def detect():
    base_proj_dir = os.path.expandvars(r"%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Resolve Project Library\Resolve Projects\Users\guest\Projects")
    if not os.path.isdir(base_proj_dir):
        return ""
    proj_dbs = glob.glob(os.path.join(base_proj_dir, "*", "Project.db"))
    if not proj_dbs:
        return ""
    newest_db = max(proj_dbs, key=os.path.getmtime)
    
    try:
        conn = sqlite3.connect(f"file:{newest_db}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute("SELECT tbl_name FROM sqlite_master WHERE type='table';")
        tables = set(r[0] for r in c.fetchall())

        for target_table in ["BtVideoInfo", "BtAudioInfo", "Sm2MpMedia"]:
            if target_table in tables:
                try:
                    c.execute(f"SELECT * FROM [{target_table}]")
                    for row in c.fetchall():
                        for val in row:
                            if isinstance(val, str) and os.path.isfile(val) and any(val.lower().endswith(ext) for ext in ('.mov', '.mp4', '.mkv', '.avi', '.m4v')):
                                return val
                            elif isinstance(val, bytes):
                                dirs = re.findall(rb'[A-Za-z]:\\[^\x00-\x1f\*\?\"\<\>\|]+', val)
                                files = re.findall(rb'[a-zA-Z0-9_\-\.\s()]+\.(?:mov|mp4|mkv|avi|m4v)', val, re.IGNORECASE)
                                for d in dirs:
                                    d_str = d.decode('utf-8', errors='ignore').strip()
                                    for f in files:
                                        f_str = f.decode('utf-8', errors='ignore').strip()
                                        candidate = os.path.join(d_str, f_str)
                                        if os.path.isfile(candidate):
                                            return candidate
                except Exception:
                    pass
    except Exception:
        pass
    return ""

if __name__ == "__main__":
    result = detect()
    if result:
        sys.stdout.write(result)
        # Also sync to current_clip.txt directly
        clip_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "current_clip.txt")
        try:
            with open(clip_file, "w", encoding="utf-8") as f:
                f.write(result)
        except Exception:
            pass
