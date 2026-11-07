import concurrent.futures
import contextlib
import gc
import glob
import json
import math
import os
import shutil
import sys
import threading
import time

import customtkinter as ctk
import cv2
import numpy as np
from PIL import Image, ImageTk
from tkinter import messagebox
import torch

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


def resource_path(relative_path):
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, relative_path)


class ResolveMagicMaskApp(ctk.CTk):
    """
    DaVinci Resolve - Magic Mask Studio
    Matches DaVinci Resolve (Color & Fusion pages) Magic Mask palette in UI,
    behavior, controls, shortcuts, and output quality.
    Powered by SAM 2 (Segment Anything Model 2) Neural Engine.
    """

    def __init__(self, initial_video=None):
        super().__init__()
        self.title("DaVinci Resolve - Magic Mask Studio")
        icon_path = resource_path(os.path.join("assets", "myrototool-icon.ico"))
        if os.path.isfile(icon_path):
            self.iconbitmap(icon_path)
        self.geometry("1300x870")
        self.minsize(1120, 760)

        # Video & Project State
        self.video_path = initial_video
        self.cap = None
        self.total_frames = 0
        self.fps = 30.0
        self.orig_w, self.orig_h = 1920, 1080
        self.disp_w, self.disp_h = 854, 480
        self.infer_w, self.infer_h = 854, 480

        # Timeline State
        self.current_frame_idx = 0
        self.current_frame_bgr = None
        self.current_raw_prob = None
        self.is_playing = False
        self.stop_tracking_flag = False

        # Multi-frame Keyframe Strokes
        # {rel_frame_idx: list of {'points': [[x,y],...], 'label': 1 or 0, 'canvas_pts': [(x,y),...]}}
        self.frame_strokes = {}
        self.active_stroke_points = []
        self.active_stroke_label = 1
        self.stroke_mode = 1  # 1 = Positive (+), 0 = Negative (-)
        self.invert_mask = False

        # Probability Map Cache across timeline: {abs_frame_idx: float32 ndarray (orig_h, orig_w)}
        self.tracked_probs = {}

        # AI Engine & Device
        self.predictor = None
        self.inference_state = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.device == "cuda":
            torch.backends.cudnn.benchmark = True
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        self.storage_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roto_storage")
        self.temp_dir = os.path.join(self.storage_dir, "temp_frames")
        self.output_dir = os.path.join(self.storage_dir, "masks")
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        self._extracted_cache = None
        self.ram_frame_cache = {}
        self.ram_cache_bytes = 0
        # Use available RAM for preview caching, but never consume the reserve
        # needed by Windows, Resolve, and SAM2 initialization.
        self.ram_cache_limit = self._get_safe_ram_cache_limit()
        self._status_job = None

        # Timeline Source & Node Sync State
        self.timeline_context = None
        self.current_timeline_clip = None
        self.timeline_clips_list = []
        self.last_sync_timestamp = 0

        # Build UI & Shortcuts
        self._build_ui()
        self._bind_shortcuts()

        # Handle automatic cleanup on application exit
        self.protocol("WM_DELETE_WINDOW", self.on_app_close)

        # Defer Resolve/database synchronization until the window has rendered.
        self.after(100, lambda: self.refresh_timeline_selection(refresh_type="all", user_initiated=False))

        # Background Live Timeline & Node Context Watcher
        self.after(1500, self._watch_timeline_context)

    @staticmethod
    def _get_safe_ram_cache_limit():
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_uint32),
                    ("memory_load", ctypes.c_uint32),
                    ("total_phys", ctypes.c_uint64),
                    ("avail_phys", ctypes.c_uint64),
                    ("total_page", ctypes.c_uint64),
                    ("avail_page", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64),
                    ("avail_virtual", ctypes.c_uint64),
                    ("avail_extended", ctypes.c_uint64),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                reserve = 4 * 1024**3
                usable = max(512 * 1024**2, status.avail_phys - reserve)
                return min(10 * 1024**3, usable)
        except Exception:
            pass
        return 4 * 1024**3

    def _watch_clip_file(self):
        try:
            tracking = str(self.btn_track_both.cget("state")) == "disabled"
            if not tracking:
                p = self._read_current_clip_file()
                if p and os.path.abspath(p) != os.path.abspath(self.video_path or ""):
                    self.load_video(p, auto_prepare=True)
        except Exception:
            pass
        self.after(1000, self._watch_clip_file)

    def _read_current_clip_file(self):
        # 1. Inspect active DaVinci Resolve project database for currently active video
        try:
            from detect_clip import detect
            db_clip = detect()
            if db_clip and os.path.isfile(db_clip):
                return db_clip
        except Exception:
            pass

        # 2. Check current_clip.txt bridge
        clip_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "current_clip.txt")
        if os.path.isfile(clip_file):
            try:
                with open(clip_file, "r", encoding="utf-8-sig") as cf:
                    p = cf.read().lstrip("\ufeff").strip()
                    if p and os.path.isfile(p):
                        return p
            except Exception:
                pass
        return None

    # -------------------------------------------------------------
    # UI CONSTRUCTION (DAVINCI RESOLVE MAGIC MASK PALETTE)
    # -------------------------------------------------------------
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ================= LEFT SIDEBAR (RESOLVE INSPECTOR) =================
        self.sidebar = ctk.CTkScrollableFrame(self, width=330, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)

        # Header
        lbl_head = ctk.CTkLabel(self.sidebar, text="Magic Mask", font=ctk.CTkFont(size=20, weight="bold"))
        lbl_head.pack(pady=(12, 1), padx=15, anchor="w")
        lbl_sub = ctk.CTkLabel(
            self.sidebar,
            text=f"DaVinci Resolve Neural Engine ({self.device.upper()})",
            font=ctk.CTkFont(size=11),
            text_color="#9ca3af"
        )
        lbl_sub.pack(pady=(0, 8), padx=15, anchor="w")

        # ================= TIMELINE SOURCE PALETTE =================
        tl_box = ctk.CTkFrame(self.sidebar, fg_color="#182234", corner_radius=8, border_width=1, border_color="#1e3a8a")
        tl_box.pack(pady=(4, 8), padx=12, fill="x")

        # Header Row: Title & Live Status
        hdr_row = ctk.CTkFrame(tl_box, fg_color="transparent")
        hdr_row.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(hdr_row, text="Timeline Source", font=ctk.CTkFont(size=12, weight="bold"), text_color="#60a5fa").pack(side="left")

        self.lbl_sync_status = ctk.CTkLabel(
            hdr_row, text="🟢 Live Sync", font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#4ade80"
        )
        self.lbl_sync_status.pack(side="right")

        # 1. Primary Action: Get Selected Timeline Clip
        self.btn_get_timeline_clip = ctk.CTkButton(
            tl_box, text="📥 Get Selected Timeline Clip", height=32,
            fg_color="#2563eb", hover_color="#1d4ed8", font=ctk.CTkFont(size=12, weight="bold"),
            command=self.on_get_selected_timeline_clip
        )
        self.btn_get_timeline_clip.pack(pady=4, padx=10, fill="x")

        # 2. Refresh Actions Row
        act_row = ctk.CTkFrame(tl_box, fg_color="transparent")
        act_row.pack(pady=2, padx=10, fill="x")

        self.btn_refresh_timeline = ctk.CTkButton(
            act_row, text="🔄 Refresh Selection", width=125, height=26,
            fg_color="#374151", hover_color="#4b5563", font=ctk.CTkFont(size=11),
            command=lambda: self.refresh_timeline_selection("timeline", user_initiated=True)
        )
        self.btn_refresh_timeline.pack(side="left", expand=True, fill="x", padx=(0, 2))

        self.btn_refresh_nodes = ctk.CTkButton(
            act_row, text="🧩 Refresh Nodes", width=115, height=26,
            fg_color="#374151", hover_color="#4b5563", font=ctk.CTkFont(size=11),
            command=lambda: self.refresh_timeline_selection("nodes", user_initiated=True)
        )
        self.btn_refresh_nodes.pack(side="left", expand=True, fill="x", padx=(2, 0))

        self.btn_refresh_all = ctk.CTkButton(
            tl_box, text="⚡ Refresh All (Timeline & Nodes)", height=24,
            fg_color="#1e293b", hover_color="#334155", text_color="#93c5fd", font=ctk.CTkFont(size=10, weight="bold"),
            command=lambda: self.refresh_timeline_selection("all", user_initiated=True)
        )
        self.btn_refresh_all.pack(pady=(2, 4), padx=10, fill="x")

        # 3. Timeline Clip Selector Dropdown
        sel_hdr = ctk.CTkFrame(tl_box, fg_color="transparent")
        sel_hdr.pack(fill="x", padx=10, pady=(4, 1))
        ctk.CTkLabel(sel_hdr, text="Timeline Clip:", font=ctk.CTkFont(size=11)).pack(side="left")

        self.cb_timeline_clips = ctk.CTkOptionMenu(
            tl_box, values=["Scanning Timeline..."], font=ctk.CTkFont(size=11),
            command=self.on_timeline_clip_dropdown_select
        )
        self.cb_timeline_clips.pack(pady=(1, 5), padx=10, fill="x")

        # 4. Metadata Display Card
        meta_box = ctk.CTkFrame(tl_box, fg_color="#0b1120", corner_radius=6)
        meta_box.pack(pady=4, padx=10, fill="x")

        self.lbl_meta_name = ctk.CTkLabel(meta_box, text="Clip: None", font=ctk.CTkFont(size=11, weight="bold"), anchor="w", text_color="#f8fafc")
        self.lbl_meta_name.pack(padx=8, pady=(4, 1), anchor="w", fill="x")

        self.lbl_meta_path = ctk.CTkLabel(meta_box, text="Linked source clip", font=ctk.CTkFont(size=10), anchor="w", text_color="#94a3b8", wraplength=270)
        self.lbl_meta_path.pack(padx=8, pady=1, anchor="w", fill="x")

        self.lbl_meta_dur = ctk.CTkLabel(meta_box, text="Duration: 0 frames", font=ctk.CTkFont(size=10), anchor="w", text_color="#cbd5e1")
        self.lbl_meta_dur.pack(padx=8, pady=1, anchor="w", fill="x")

        self.lbl_meta_range = ctk.CTkLabel(meta_box, text="Source In: 0 | Out: 0", font=ctk.CTkFont(size=10), anchor="w", text_color="#cbd5e1")
        self.lbl_meta_range.pack(padx=8, pady=1, anchor="w", fill="x")

        self.lbl_meta_fmt = ctk.CTkLabel(meta_box, text="Format: -", font=ctk.CTkFont(size=10), anchor="w", text_color="#cbd5e1")
        self.lbl_meta_fmt.pack(padx=8, pady=1, anchor="w", fill="x")

        self.lbl_meta_nodes = ctk.CTkLabel(meta_box, text="Nodes: None", font=ctk.CTkFont(size=10), anchor="w", text_color="#38bdf8", wraplength=270)
        self.lbl_meta_nodes.pack(padx=8, pady=(1, 6), anchor="w", fill="x")

        # 5. Segment Processing Switch
        self.chk_segment_only = ctk.CTkCheckBox(
            tl_box, text="Process Selected Segment Only", font=ctk.CTkFont(size=11),
            command=self.on_toggle_segment_only
        )
        self.chk_segment_only.pack(pady=(3, 8), padx=10, anchor="w")
        self.chk_segment_only.select()

        # Legacy sync button reference for backward compatibility
        self.btn_sync = self.btn_refresh_timeline

        # 1. Mask Mode (Person Mask / Object Mask)
        ctk.CTkLabel(self.sidebar, text="Mask Mode:", font=ctk.CTkFont(size=12, weight="bold")).pack(pady=(10, 2), padx=15, anchor="w")
        self.mode_menu = ctk.CTkSegmentedButton(
            self.sidebar,
            values=["Person Mask", "Object Mask"]
        )
        self.mode_menu.set("Person Mask")
        self.mode_menu.pack(pady=2, padx=15, fill="x")

        # 2. Quality & Consistency
        qc_box = ctk.CTkFrame(self.sidebar, fg_color="#1f2937", corner_radius=6)
        qc_box.pack(pady=(8, 4), padx=15, fill="x")

        q_row = ctk.CTkFrame(qc_box, fg_color="transparent")
        q_row.pack(pady=(6, 2), padx=10, fill="x")
        ctk.CTkLabel(q_row, text="Quality:", font=ctk.CTkFont(size=12)).pack(side="left")
        self.quality_menu = ctk.CTkOptionMenu(q_row, width=120, values=["Faster", "Better", "Best"])
        self.quality_menu.set("Faster")
        self.quality_menu.pack(side="right")

        c_row = ctk.CTkFrame(qc_box, fg_color="transparent")
        c_row.pack(pady=(2, 6), padx=10, fill="x")
        ctk.CTkLabel(c_row, text="Consistency:", font=ctk.CTkFont(size=12)).pack(side="left")
        self.consistency_menu = ctk.CTkOptionMenu(c_row, width=120, values=["Faster", "Better", "Best"])
        self.consistency_menu.set("Faster")
        self.consistency_menu.pack(side="right")

        # 3. Selection Tools (Add / Subtract / Invert / Clear)
        ctk.CTkLabel(self.sidebar, text="Selection Tools (الفرشاة):", font=ctk.CTkFont(size=12, weight="bold")).pack(pady=(10, 2), padx=15, anchor="w")
        tool_row = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        tool_row.pack(pady=2, padx=15, fill="x")

        self.btn_add = ctk.CTkButton(
            tool_row, text="Add (+)", width=75, fg_color="#15803d", hover_color="#166534",
            command=lambda: self.set_stroke_mode(1)
        )
        self.btn_add.pack(side="left", padx=(0, 3), expand=True, fill="x")

        self.btn_sub = ctk.CTkButton(
            tool_row, text="Subtract (-)", width=85, fg_color="#374151", hover_color="#4b5563",
            command=lambda: self.set_stroke_mode(0)
        )
        self.btn_sub.pack(side="left", padx=3, expand=True, fill="x")

        self.btn_inv = ctk.CTkButton(
            tool_row, text="Invert", width=65, fg_color="#374151", hover_color="#4b5563",
            command=self.toggle_invert
        )
        self.btn_inv.pack(side="left", padx=(3, 0), expand=True, fill="x")

        clear_row = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        clear_row.pack(pady=3, padx=15, fill="x")
        self.btn_clear_frame = ctk.CTkButton(
            clear_row, text="Clear Frame", width=120, height=26, fg_color="#4b5563", hover_color="#374151",
            command=self.clear_current_frame_strokes
        )
        self.btn_clear_frame.pack(side="left", padx=(0, 3), expand=True, fill="x")
        self.btn_clear_all = ctk.CTkButton(
            clear_row, text="Clear All Masks", width=120, height=26, fg_color="#7f1d1d", hover_color="#991b1b",
            command=self.clear_all_masks
        )
        self.btn_clear_all.pack(side="left", padx=(3, 0), expand=True, fill="x")

        # 4. Tracking Range & Prepare
        ctk.CTkLabel(self.sidebar, text="Tracking Range (النطاق):", font=ctk.CTkFont(size=12, weight="bold")).pack(pady=(10, 2), padx=15, anchor="w")
        rf = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        rf.pack(pady=2, padx=15, fill="x")
        self.entry_start = ctk.CTkEntry(rf, width=90, placeholder_text="Start: 0")
        self.entry_start.pack(side="left", padx=(0, 4), expand=True, fill="x")
        self.entry_end = ctk.CTkEntry(rf, width=90, placeholder_text="End: Total")
        self.entry_end.pack(side="left", padx=(4, 0), expand=True, fill="x")

        self.btn_init_ai = ctk.CTkButton(
            self.sidebar, text="⚡ Prepare Engine", fg_color="#d97706", hover_color="#b45309", height=32,
            command=self.prepare_ai_workspace
        )
        self.btn_init_ai.pack(pady=6, padx=15, fill="x")

        # 5. Tracking Controls (Forward / Backward / Both / Stop)
        ctk.CTkLabel(self.sidebar, text="Tracking Controls (التتبع):", font=ctk.CTkFont(size=12, weight="bold")).pack(pady=(8, 2), padx=15, anchor="w")
        t_row1 = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        t_row1.pack(pady=2, padx=15, fill="x")
        self.btn_track_both = ctk.CTkButton(
            t_row1, text="⏪ Track Both Directions ⏩", fg_color="#2563eb", hover_color="#1d4ed8", height=36,
            font=ctk.CTkFont(weight="bold"),
            command=lambda: self.start_tracking("both")
        )
        self.btn_track_both.pack(fill="x")

        t_row2 = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        t_row2.pack(pady=3, padx=15, fill="x")
        self.btn_track_back = ctk.CTkButton(
            t_row2, text="⏪ Reverse", fg_color="#1d4ed8", hover_color="#1e40af", height=30,
            command=lambda: self.start_tracking("backward")
        )
        self.btn_track_back.pack(side="left", padx=(0, 3), expand=True, fill="x")
        self.btn_track_fwd = ctk.CTkButton(
            t_row2, text="Forward ⏩", fg_color="#1d4ed8", hover_color="#1e40af", height=30,
            command=lambda: self.start_tracking("forward")
        )
        self.btn_track_fwd.pack(side="left", padx=(3, 0), expand=True, fill="x")

        self.btn_stop = ctk.CTkButton(
            self.sidebar, text="⏹ Stop Tracking", fg_color="#dc2626", hover_color="#b91c1c", height=28,
            command=self.stop_tracking
        )
        self.btn_stop.pack(pady=(2, 8), padx=15, fill="x")

        # 6. MATTE FINESSE CONTROLS (RESOLVE ALGORITHMS)
        finesse_hdr = ctk.CTkLabel(self.sidebar, text="Matte Finesse (معالجة الحواف):", font=ctk.CTkFont(size=13, weight="bold"))
        finesse_hdr.pack(pady=(10, 2), padx=15, anchor="w")

        finesse_container = ctk.CTkFrame(self.sidebar, fg_color="#182234", corner_radius=6)
        finesse_container.pack(pady=2, padx=15, fill="x")

        self.slider_clean_black = self._create_finesse_slider(finesse_container, "Clean Black", 0, 100, default=5)
        self.slider_clean_white = self._create_finesse_slider(finesse_container, "Clean White", 0, 100, default=5)
        self.slider_shrink_grow = self._create_finesse_slider(finesse_container, "Shrink / Grow", -100, 100, default=0)
        self.slider_blur = self._create_finesse_slider(finesse_container, "Blur Radius (Feather)", 0, 100, default=6)

        # 7. Preview & Export
        ctk.CTkLabel(self.sidebar, text="Preview & Export:", font=ctk.CTkFont(size=12, weight="bold")).pack(pady=(12, 2), padx=15, anchor="w")
        self.preview_mode = ctk.CTkOptionMenu(
            self.sidebar,
            values=["Overlay (Highlight)", "Alpha Matte (B&W)", "Output (Cutout)", "Green Screen"],
            command=lambda _: self.render_current_display()
        )
        self.preview_mode.set("Overlay (Highlight)")
        self.preview_mode.pack(pady=2, padx=15, fill="x")

        self.btn_export = ctk.CTkButton(
            self.sidebar, text="Render Mask (Manual Import)", height=42,
            fg_color="#16a34a", hover_color="#15803d", font=ctk.CTkFont(size=14, weight="bold"),
            command=self.export_mask
        )
        self.btn_export.pack(pady=(12, 4), padx=15, fill="x")

        # 8. Cache & Temp Management
        self.btn_clear_cache = ctk.CTkButton(
            self.sidebar, text="🗑 Clear Cache & Temp (مسح التيمب)", height=28,
            fg_color="#334155", hover_color="#475569", text_color="#f1f5f9", font=ctk.CTkFont(size=11, weight="bold"),
            command=self.manual_clear_cache
        )
        self.btn_clear_cache.pack(pady=(2, 6), padx=15, fill="x")

        # Progress bar & Status
        self.progress = ctk.CTkProgressBar(self.sidebar)
        self.progress.pack(pady=4, padx=15, fill="x")
        self.progress.set(0)

        self.lbl_status = ctk.CTkLabel(self.sidebar, text="Ready.", wraplength=280, text_color="#9ca3af", font=ctk.CTkFont(size=11))
        self.lbl_status.pack(pady=4, padx=15)

        # ================= MAIN AREA (VIEWPORT & TIMELINE) =================
        self.main_area = ctk.CTkFrame(self, fg_color="#111827")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        self.main_area.grid_rowconfigure(0, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)

        # Canvas Frame
        self.canvas_frame = ctk.CTkFrame(self.main_area, fg_color="#0b0f19")
        self.canvas_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self.canvas_frame.pack_propagate(False)

        self.canvas = ctk.CTkCanvas(self.canvas_frame, bg="#0b0f19", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(expand=True)

        # Respond to window resize to re-scale the preview
        self.canvas_frame.bind("<Configure>", lambda e: self.after(100, self._on_canvas_resize))

        # Canvas Mouse Bindings for Drawing Strokes
        self.canvas.bind("<ButtonPress-1>", lambda e: self.on_stroke_start(e, label=self.stroke_mode))
        self.canvas.bind("<B1-Motion>", self.on_stroke_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_stroke_end)

        self.canvas.bind("<ButtonPress-3>", lambda e: self.on_stroke_start(e, label=0))  # Right click always subtracts (-)
        self.canvas.bind("<B3-Motion>", self.on_stroke_motion)
        self.canvas.bind("<ButtonRelease-3>", self.on_stroke_end)

        # ================= BOTTOM TIMELINE SCRUBBER =================
        self.timeline_bar = ctk.CTkFrame(self.main_area, height=84, fg_color="#1f2937")
        self.timeline_bar.grid(row=1, column=0, sticky="ew", padx=5, pady=5)

        # Scrubber Slider
        self.time_slider = ctk.CTkSlider(self.timeline_bar, from_=0, to=100, number_of_steps=100, command=self.on_slider_scrub)
        self.time_slider.pack(fill="x", padx=15, pady=(8, 4))
        self.time_slider.set(0)

        # Timeline Transport Controls
        tb_controls = ctk.CTkFrame(self.timeline_bar, fg_color="transparent")
        tb_controls.pack(fill="x", padx=15, pady=(2, 6))

        self.btn_step_prev10 = ctk.CTkButton(tb_controls, text="⏮ -10f", width=55, height=26, fg_color="#374151", command=lambda: self.step_frame(-10))
        self.btn_step_prev10.pack(side="left", padx=2)
        self.btn_step_prev = ctk.CTkButton(tb_controls, text="◀ -1f", width=50, height=26, fg_color="#374151", command=lambda: self.step_frame(-1))
        self.btn_step_prev.pack(side="left", padx=2)

        self.btn_play = ctk.CTkButton(tb_controls, text="▶ Play", width=65, height=26, fg_color="#2563eb", command=self.toggle_play)
        self.btn_play.pack(side="left", padx=4)

        self.btn_step_next = ctk.CTkButton(tb_controls, text="+1f ▶", width=50, height=26, fg_color="#374151", command=lambda: self.step_frame(1))
        self.btn_step_next.pack(side="left", padx=2)
        self.btn_step_next10 = ctk.CTkButton(tb_controls, text="+10f ⏭", width=55, height=26, fg_color="#374151", command=lambda: self.step_frame(10))
        self.btn_step_next10.pack(side="left", padx=2)

        # Timecode & Keyframe Indicator
        self.lbl_frame_info = ctk.CTkLabel(tb_controls, text="Frame: 0 / 0  (00:00:00:00)", font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl_frame_info.pack(side="right", padx=10)

        self.lbl_kf_badge = ctk.CTkLabel(tb_controls, text="", text_color="#eab308", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_kf_badge.pack(side="right", padx=5)

    def _create_finesse_slider(self, parent, label_text, from_, to, default):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(pady=1, padx=8, fill="x")
        lbl = ctk.CTkLabel(f, text=f"{label_text}:", font=ctk.CTkFont(size=11))
        lbl.pack(anchor="w")
        slider = ctk.CTkSlider(
            f, from_=from_, to=to,
            number_of_steps=max(1, abs(int(to - from_))),
            command=lambda _: self.render_current_display()
        )
        slider.set(default)
        slider.pack(fill="x", pady=(1, 3))
        return slider

    def _bind_shortcuts(self):
        self.bind("<space>", lambda _: self.toggle_play())
        self.bind("<Left>", lambda _: self.step_frame(-1))
        self.bind("<Right>", lambda _: self.step_frame(1))
        self.bind("<Shift-Left>", lambda _: self.step_frame(-10))
        self.bind("<Shift-Right>", lambda _: self.step_frame(10))
        self.bind("a", lambda _: self.set_stroke_mode(1))
        self.bind("A", lambda _: self.set_stroke_mode(1))
        self.bind("+", lambda _: self.set_stroke_mode(1))
        self.bind("s", lambda _: self.set_stroke_mode(0))
        self.bind("S", lambda _: self.set_stroke_mode(0))
        self.bind("-", lambda _: self.set_stroke_mode(0))
        self.bind("i", lambda _: self.toggle_invert())
        self.bind("I", lambda _: self.toggle_invert())
        self.bind("c", lambda _: self.clear_current_frame_strokes())
        self.bind("C", lambda _: self.clear_current_frame_strokes())

    def set_status(self, text, persistent=False):
        def update():
            if self._status_job is not None:
                try:
                    self.after_cancel(self._status_job)
                except Exception:
                    pass
            self.lbl_status.configure(text=text)
            self._status_job = None if persistent else self.after(
                3000, lambda: self.lbl_status.configure(text="")
            )
        self.after(0, update)

    def set_stroke_mode(self, mode):
        self.stroke_mode = mode
        if mode == 1:
            self.btn_add.configure(fg_color="#15803d")
            self.btn_sub.configure(fg_color="#374151")
        else:
            self.btn_add.configure(fg_color="#374151")
            self.btn_sub.configure(fg_color="#dc2626")

    def toggle_invert(self):
        self.invert_mask = not self.invert_mask
        self.btn_inv.configure(fg_color="#d97706" if self.invert_mask else "#374151")
        self.render_current_display()

    # -------------------------------------------------------------
    # TIMELINE & NODE SYNCHRONIZATION
    # -------------------------------------------------------------
    def on_get_selected_timeline_clip(self):
        """Action for 'Get Selected Timeline Clip' button."""
        self.refresh_timeline_selection(refresh_type="all", user_initiated=True)

    def on_timeline_clip_dropdown_select(self, choice_text):
        """User switched clip via the Timeline Clip Selector dropdown."""
        for clip in self.timeline_clips_list:
            display_title = f"[{clip.get('track', 'V1')}] {clip.get('name', 'Clip')} ({clip.get('start', 0)}-{clip.get('end', 0)})"
            if choice_text == display_title or choice_text == clip.get("name"):
                self.load_timeline_clip(clip, auto_prepare=True)
                break

    def on_toggle_segment_only(self):
        """Toggle processing between the trimmed Timeline segment vs full video."""
        if not self.cap or self.total_frames <= 0:
            return
        if self.chk_segment_only.get() and self.current_timeline_clip:
            s_in = int(self.current_timeline_clip.get("source_in", 0))
            s_out = int(self.current_timeline_clip.get("source_out", self.total_frames - 1))
            s_out = min(self.total_frames - 1, max(s_in, s_out))
            self.entry_start.delete(0, "end")
            self.entry_start.insert(0, str(s_in))
            self.entry_end.delete(0, "end")
            self.entry_end.insert(0, str(s_out))
            self.time_slider.set(s_in)
            self.seek_and_show_frame(s_in)
            self.set_status(f"Segment range applied: frames {s_in} to {s_out}")
        else:
            self.entry_start.delete(0, "end")
            self.entry_start.insert(0, "0")
            self.entry_end.delete(0, "end")
            self.entry_end.insert(0, str(self.total_frames - 1))
            self.time_slider.set(0)
            self.seek_and_show_frame(0)
            self.set_status(f"Full source range applied: 0 to {self.total_frames - 1}")

    def load_timeline_clip(self, clip, auto_prepare=True):
        """Loads a Timeline clip, sets source In/Out points, updates displays, and resets session."""
        if not clip:
            return

        file_path = clip.get("file_path")
        if not file_path or not os.path.isfile(file_path):
            self.lbl_sync_status.configure(text="🔴 File Missing", text_color="#ef4444")
            self.lbl_meta_path.configure(text="Source file unavailable", text_color="#ef4444")
            messagebox.showerror(
                "Source File Missing",
                f"Source video file for '{clip.get('name')}' was not found on disk:\n{file_path}\n\nVerify that the media is linked in DaVinci Resolve."
            )
            return

        self.current_timeline_clip = clip

        # Update metadata card displays
        self.lbl_meta_name.configure(text=f"Clip: {clip.get('name', 'Unknown')} ({clip.get('track', 'V1')})")
        self.lbl_meta_path.configure(text="Linked source clip", text_color="#94a3b8")
        dur_tc = clip.get("duration_tc", "")
        self.lbl_meta_dur.configure(text=f"Duration: {clip.get('duration', 0)} frames ({dur_tc})")
        self.lbl_meta_range.configure(text=f"Source In: {clip.get('source_in', 0)} | Out: {clip.get('source_out', 0)} | TL Start: {clip.get('start', 0)}")
        self.lbl_meta_fmt.configure(text=f"Format: {clip.get('resolution', '-')} @ {clip.get('fps', 30.0)} FPS")

        # Node information display
        c_nodes = clip.get("color_nodes", [])
        f_tools = clip.get("fusion_tools", [])
        node_parts = []
        if c_nodes:
            node_parts.append(f"Color: {len(c_nodes)} node(s)")
        if f_tools:
            tool_names = [t.get("name", "") for t in f_tools if t.get("name")]
            node_parts.append(f"Fusion: {' -> '.join(tool_names[:4])}")
        if not node_parts:
            node_parts.append("Environment: Edit Page Timeline")
        self.lbl_meta_nodes.configure(text=" | ".join(node_parts))

        # Load video if different or not loaded
        needs_reload = (self.video_path != file_path) or (self.cap is None)
        if needs_reload:
            self.load_video(file_path, auto_prepare=False)

        # Apply segment range
        if self.chk_segment_only.get():
            s_in = int(clip.get("source_in", 0))
            s_out = int(clip.get("source_out", self.total_frames - 1))
            s_out = min(self.total_frames - 1, max(s_in, s_out))
            self.entry_start.delete(0, "end")
            self.entry_start.insert(0, str(s_in))
            self.entry_end.delete(0, "end")
            self.entry_end.insert(0, str(s_out))
            self.time_slider.set(s_in)
            self.seek_and_show_frame(s_in)

        self.lbl_sync_status.configure(text=f"🟢 {clip.get('name', 'Clip')}", text_color="#4ade80")
        self.set_status(f"Synchronized Timeline Clip: {clip.get('name')} (Track {clip.get('track')})")

    def refresh_timeline_selection(self, refresh_type="all", user_initiated=False):
        """Unified refresh method for Timeline selection, Nodes, or All."""
        try:
            import timeline_sync
            ctx = timeline_sync.get_timeline_context()
        except Exception as e:
            self.lbl_sync_status.configure(text="🔴 Sync Error", text_color="#ef4444")
            self.set_status(f"Sync error: {e}", persistent=True)
            if user_initiated:
                messagebox.showerror("Sync Error", f"Error querying DaVinci Resolve context:\n{e}")
            return

        self.timeline_context = ctx
        all_clips = ctx.get("all_clips", [])
        self.timeline_clips_list = all_clips

        if not all_clips:
            self.lbl_sync_status.configure(text="🔴 No Timeline Clips", text_color="#ef4444")
            self.cb_timeline_clips.configure(values=["No Clips Found"])
            self.cb_timeline_clips.set("No Clips Found")
            if user_initiated:
                messagebox.showwarning(
                    "No Clip Found",
                    "No video clips were found on the active DaVinci Resolve Timeline.\n\nPlease open DaVinci Resolve, add a video clip to the Timeline on the Edit or Fusion page, and try again."
                )
            return

        # Build dropdown options
        options = []
        for c in all_clips:
            title = f"[{c.get('track', 'V1')}] {c.get('name', 'Clip')} ({c.get('start', 0)}-{c.get('end', 0)})"
            options.append(title)
        self.cb_timeline_clips.configure(values=options)

        # Determine target active clip
        active = ctx.get("active_clip")
        if not active and all_clips:
            active = all_clips[0]

        if active:
            active_title = f"[{active.get('track', 'V1')}] {active.get('name', 'Clip')} ({active.get('start', 0)}-{active.get('end', 0)})"
            self.cb_timeline_clips.set(active_title)

            # Refresh nodes specifically if requested
            if refresh_type == "nodes" and self.current_timeline_clip:
                self.current_timeline_clip["color_nodes"] = ctx.get("color_nodes", [])
                self.current_timeline_clip["fusion_tools"] = ctx.get("fusion_tools", [])
                c_nodes = self.current_timeline_clip["color_nodes"]
                f_tools = self.current_timeline_clip["fusion_tools"]
                n_str = f"Color: {len(c_nodes)} node(s)" if c_nodes else "Edit Page"
                if f_tools:
                    tool_names = [t.get("name", "") for t in f_tools if t.get("name")]
                    n_str += f" | Fusion: {' -> '.join(tool_names[:4])}"
                self.lbl_meta_nodes.configure(text=n_str)
                self.lbl_sync_status.configure(text="🟢 Nodes Updated", text_color="#38bdf8")
                self.set_status("Node graph context refreshed.")
                return

            self.load_timeline_clip(active, auto_prepare=True)
            self.lbl_sync_status.configure(
                text=f"🟢 {ctx.get('source_type', 'Connected')}",
                text_color="#4ade80"
            )

    def _watch_timeline_context(self):
        try:
            tracking = str(self.btn_track_both.cget("state")) == "disabled"
            if not tracking:
                import timeline_sync
                ctx_file = timeline_sync.CONTEXT_FILE
                if os.path.isfile(ctx_file):
                    mtime = os.path.getmtime(ctx_file)
                    if mtime > self.last_sync_timestamp:
                        self.last_sync_timestamp = mtime
                        self.refresh_timeline_selection(refresh_type="all", user_initiated=False)
        except Exception:
            pass
        self.after(1500, self._watch_timeline_context)

    # -------------------------------------------------------------
    # DYNAMIC VIEWPORT SCALING
    # -------------------------------------------------------------
    def _update_display_scaling(self):
        """Dynamically compute display size to fill the canvas frame area."""
        self.update_idletasks()
        avail_w = self.canvas_frame.winfo_width() - 20
        avail_h = self.canvas_frame.winfo_height() - 20
        # Fallback if frame not rendered yet
        if avail_w < 200:
            avail_w = max(600, self.winfo_width() - 380)
        if avail_h < 200:
            avail_h = max(500, self.winfo_height() - 180)

        scale = min(avail_w / max(1, self.orig_w), avail_h / max(1, self.orig_h))
        self.disp_w = max(320, int(self.orig_w * scale))
        self.disp_h = max(240, int(self.orig_h * scale))

    def _on_canvas_resize(self, event=None):
        """Called when the main window or canvas frame is resized."""
        if not self.cap or self.total_frames <= 0:
            return
        old_w, old_h = self.disp_w, self.disp_h
        self._update_display_scaling()
        if (old_w, old_h) != (self.disp_w, self.disp_h):
            self.canvas.config(width=self.disp_w, height=self.disp_h)
            self.render_current_display()

    # -------------------------------------------------------------
    # VIDEO LOADING & SYNC
    # -------------------------------------------------------------
    def sync_from_resolve(self):
        p = self._read_current_clip_file()
        if p and os.path.isfile(p):
            self.load_video(p, auto_prepare=True)
            self.set_status(f"Synced clip from Resolve: {os.path.basename(p)}")
        else:
            messagebox.showinfo("Resolve Sync", "لم يتم العثور على مسار كليب من Resolve.\nتأكد من وجود كليب فيديو في المشروع النشط أو توصيل عقدة فيديو بمدخل LocalAIRoto.")

    def load_video(self, path, auto_prepare=False):
        if self.cap:
            self.cap.release()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("Error", f"Could not open video file:\n{path}")
            return

        self.video_path = path
        self.cap = cap
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # Viewport display scaling — dynamically fit canvas area
        self._update_display_scaling()
        self.canvas.config(width=self.disp_w, height=self.disp_h)

        # Clean previous temp frames automatically
        self.cleanup_temp_files(silent=True)

        # Scrubber slider bounds
        self.time_slider.configure(from_=0, to=max(1, self.total_frames - 1), number_of_steps=max(1, self.total_frames - 1))
        self.time_slider.set(0)

        self.entry_start.delete(0, "end")
        self.entry_start.insert(0, "0")
        self.entry_end.delete(0, "end")
        self.entry_end.insert(0, str(self.total_frames - 1))

        # Reset session state
        self.frame_strokes.clear()
        self.tracked_probs.clear()
        self.current_frame_idx = 0
        self.inference_state = None
        self.ram_frame_cache.clear()
        self.ram_cache_bytes = 0

        self.seek_and_show_frame(0)
        self.set_status(f"Loaded: {os.path.basename(path)} ({self.total_frames} frames, {self.orig_w}x{self.orig_h})")

    # -------------------------------------------------------------
    # CACHE & TEMP MANAGEMENT (AUTO & MANUAL CLEANUP)
    # -------------------------------------------------------------
    def manual_clear_cache(self):
        """Action for 'Clear Cache & Temp' button."""
        self.cleanup_temp_files(silent=False)

    def on_app_close(self):
        """Automatically clean up all temp files and free GPU memory on app exit."""
        try:
            self.cleanup_temp_files(silent=True)
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass
        self.destroy()

    def cleanup_temp_files(self, silent=False):
        """
        Cleans up disk temporary frames, clears memory cache, and frees GPU VRAM.
        """
        freed_bytes = 0
        deleted_count = 0
        if os.path.isdir(self.temp_dir):
            for f in glob.glob(os.path.join(self.temp_dir, "*")):
                try:
                    size = os.path.getsize(f)
                    os.remove(f)
                    freed_bytes += size
                    deleted_count += 1
                except OSError:
                    pass

        # Clear probability maps cache & strokes
        self.tracked_probs.clear()
        self.current_raw_prob = None
        self.frame_strokes.clear()
        self._extracted_cache = None
        self.ram_frame_cache.clear()
        self.ram_cache_bytes = 0

        # Reset SAM 2 state
        if self.inference_state is not None and self.predictor is not None:
            try:
                self.predictor.reset_state(self.inference_state)
            except Exception:
                pass
            self.inference_state = None

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        freed_mb = round(freed_bytes / (1024 * 1024), 2)
        msg = f"تم مسح الكاش والتيمب بنجاح: {deleted_count} ملف ({freed_mb} MB)."
        self.set_status(msg)

        if not silent:
            messagebox.showinfo("مسح الكاش والمؤقتات", f"{msg}\nتم تحرير الذاكرة والكاش بالكامل.")

    def _get_range(self):
        try:
            start = max(0, int(self.entry_start.get() or 0))
        except ValueError:
            start = 0
        try:
            end = min(self.total_frames - 1, int(self.entry_end.get() or (self.total_frames - 1)))
        except ValueError:
            end = max(0, self.total_frames - 1)
        if end < start:
            end = start
        return start, end

    def amp(self):
        return torch.autocast("cuda", dtype=torch.float16) if self.device == "cuda" else contextlib.nullcontext()

    # -------------------------------------------------------------
    # AI WORKSPACE PREPARATION (EXTRACT FRAMES & INIT SAM 2)
    # -------------------------------------------------------------
    def prepare_ai_workspace(self):
        if not self.video_path:
            messagebox.showwarning("Notice", "Open a clip first.")
            return
        threading.Thread(target=self._init_ai_worker, daemon=True).start()

    def _init_ai_worker(self):
        try:
            self.btn_init_ai.configure(state="disabled")
            start_f, end_f = self._get_range()
            total_extract = end_f - start_f + 1

            # Quality preset configuration
            q = self.quality_menu.get()
            if q == "Faster":
                # Keep tracking at the smallest practical frame size. The
                # probability map is upscaled only when previewing/exporting.
                target_max = 384
                jpeg_q = 70
            elif q == "Better":
                target_max = 768
                jpeg_q = 75
            else:
                target_max = 1024
                jpeg_q = 80

            factor = min(1.0, target_max / max(self.orig_w, self.orig_h))
            self.infer_w = int(self.orig_w * factor)
            self.infer_h = int(self.orig_h * factor)

            cache_key = (self.video_path, start_f, end_f, self.infer_w, self.infer_h)
            existing_frames = len(glob.glob(os.path.join(self.temp_dir, "*.jpg"))) if os.path.isdir(self.temp_dir) else 0

            # Check if frames are already extracted and match current parameters
            if self._extracted_cache == cache_key and existing_frames >= total_extract:
                self.set_status(f"Using cached frames ({total_extract} frames)...")
            else:
                self.set_status(f"Extracting {total_extract} frames for Neural Engine ({self.infer_w}x{self.infer_h})...")

                # Clean previous temp frames safely
                if os.path.exists(self.temp_dir):
                    for f in glob.glob(os.path.join(self.temp_dir, "*")):
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                os.makedirs(self.temp_dir, exist_ok=True)
                self.ram_frame_cache.clear()
                self.ram_cache_bytes = 0

                cap = cv2.VideoCapture(self.video_path)
                cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)

                # High-speed parallel frame writing using ThreadPoolExecutor
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    for count in range(total_extract):
                        ok, frame = cap.read()
                        if not ok:
                            break
                        small = cv2.resize(frame, (self.infer_w, self.infer_h), interpolation=cv2.INTER_LINEAR)
                        frame_bytes = small.nbytes
                        if self.ram_cache_bytes + frame_bytes <= self.ram_cache_limit:
                            self.ram_frame_cache[count] = small
                            self.ram_cache_bytes += frame_bytes
                        f_path = os.path.join(self.temp_dir, f"{count:05d}.jpg")
                        executor.submit(cv2.imwrite, f_path, small, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_q])
                        if count % 25 == 0 or count == total_extract - 1:
                            self.after(0, lambda c=count: self.progress.set((c + 1) / total_extract))

                cap.release()
                self._extracted_cache = cache_key

            self.set_status(f"Initializing SAM 2 Neural Engine on {self.device.upper()}...")
            if self.predictor is None:
                from sam2.build_sam import build_sam2_video_predictor
                root = os.path.dirname(os.path.abspath(__file__))
                cfg = "configs/sam2/sam2_hiera_t.yaml"
                ckpt = os.path.join(root, "sam2_hiera_tiny.pt")
                self.predictor = build_sam2_video_predictor(cfg, ckpt, device=self.device)

            if self.device == "cuda":
                torch.cuda.empty_cache()
            gc.collect()

            # SAM2 needs the largest contiguous allocation during init. Decode
            # frames remain on disk and can be cached again after init succeeds.
            self.ram_frame_cache.clear()
            self.ram_cache_bytes = 0
            gc.collect()
            with torch.inference_mode(), self.amp():
                self.inference_state = self.predictor.init_state(video_path=self.temp_dir)

            self.after(0, self.render_current_display)
            self.set_status(f"Ready! Engine prepared for frames {start_f} to {end_f}. Draw strokes on target.")
        except Exception as e:
            self.ram_frame_cache.clear()
            self.ram_cache_bytes = 0
            gc.collect()
            if self.device == "cuda":
                torch.cuda.empty_cache()
            message = str(e)
            if "allocate" in message.lower() or "memory" in message.lower():
                message = "Not enough memory. Choose Faster quality and Prepare again."
            self.set_status(f"Preparation error: {message}", persistent=True)
        finally:
            self.after(0, lambda: self.btn_init_ai.configure(state="normal"))

    # -------------------------------------------------------------
    # STROKE INTERACTION & KEYFRAME PROMPTING
    # -------------------------------------------------------------
    def on_stroke_start(self, event, label):
        if self.inference_state is None:
            messagebox.showinfo("Notice", "Click 'Prepare Engine' first before drawing strokes.")
            return

        self.active_stroke_label = label
        self.active_stroke_points = [(event.x, event.y)]
        self.last_mouse_pos = (event.x, event.y)

    def on_stroke_motion(self, event):
        if not self.active_stroke_points:
            return
        lx, ly = self.last_mouse_pos
        dist = math.hypot(event.x - lx, event.y - ly)
        if dist >= 6:
            self.active_stroke_points.append((event.x, event.y))
            color = "#22c55e" if self.active_stroke_label == 1 else "#ef4444"
            self.canvas.create_line(lx, ly, event.x, event.y, fill=color, width=4, capstyle="round", smooth=True)
            self.last_mouse_pos = (event.x, event.y)

    def on_stroke_end(self, event):
        if not self.active_stroke_points or self.inference_state is None:
            return

        start_f, _ = self._get_range()
        rel_f = self.current_frame_idx - start_f
        if rel_f < 0:
            return

        # Sample points along brush stroke for SAM 2 prompt
        sampled_pts = []
        step = max(1, len(self.active_stroke_points) // 6)
        for i in range(0, len(self.active_stroke_points), step):
            sx, sy = self.active_stroke_points[i]
            ix = int(sx * (self.infer_w / self.disp_w))
            iy = int(sy * (self.infer_h / self.disp_h))
            sampled_pts.append([ix, iy])

        if rel_f not in self.frame_strokes:
            self.frame_strokes[rel_f] = []

        self.frame_strokes[rel_f].append({
            "points": sampled_pts,
            "label": self.active_stroke_label,
            "canvas_pts": list(self.active_stroke_points)
        })

        self.active_stroke_points = []
        self._update_sam_keyframe(self.current_frame_idx, rel_f)

    def _convert_logits_to_prob(self, logits):
        """
        Keep tracking masks at inference resolution. Upscaling every frame to the
        source resolution during propagation is a major CPU/GPU synchronization cost.
        """
        if logits.ndim == 3:
            logits_t = logits[0:1].unsqueeze(0)
        elif logits.ndim == 4:
            logits_t = logits[0:1]
        else:
            logits_t = logits.view(1, 1, logits.shape[-2], logits.shape[-1])

        if logits_t.shape[-2:] != (self.infer_h, self.infer_w):
            prob_t = torch.nn.functional.interpolate(
                logits_t,
                size=(self.infer_h, self.infer_w),
                mode="bilinear",
                align_corners=False
            )
        else:
            prob_t = logits_t
        return torch.sigmoid(prob_t).squeeze().cpu().numpy().astype(np.float32)

    def _edge_aware_upscale(self, prob, guide_frame=None):
        """Upscale the low-resolution probability map while following source edges."""
        if prob is None:
            return None
        if prob.shape[:2] == (self.orig_h, self.orig_w) or guide_frame is None:
            return cv2.resize(prob, (self.orig_w, self.orig_h), interpolation=cv2.INTER_CUBIC)

        upsampled = cv2.resize(
            prob.astype(np.float32),
            (self.orig_w, self.orig_h),
            interpolation=cv2.INTER_CUBIC
        )
        guide = cv2.cvtColor(guide_frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        radius = max(2, min(6, round(max(self.orig_w, self.orig_h) / 320)))
        eps = 0.0015

        mean_guide = cv2.boxFilter(guide, -1, (radius, radius), normalize=True)
        mean_prob = cv2.boxFilter(upsampled, -1, (radius, radius), normalize=True)
        corr_guide = cv2.boxFilter(guide * guide, -1, (radius, radius), normalize=True)
        corr_guided = cv2.boxFilter(guide * upsampled, -1, (radius, radius), normalize=True)
        variance = corr_guide - mean_guide * mean_guide
        covariance = corr_guided - mean_guide * mean_prob
        coeff_a = covariance / (variance + eps)
        coeff_b = mean_prob - coeff_a * mean_guide
        mean_a = cv2.boxFilter(coeff_a, -1, (radius, radius), normalize=True)
        mean_b = cv2.boxFilter(coeff_b, -1, (radius, radius), normalize=True)
        return np.clip(mean_a * guide + mean_b, 0.0, 1.0)

    def _fullres_prob(self, prob, guide_frame=None):
        return self._edge_aware_upscale(prob, guide_frame)

    def _update_sam_keyframe(self, abs_f, rel_f):
        if self.inference_state is None or rel_f not in self.frame_strokes:
            return

        all_pts, all_lbls = [], []
        for stroke in self.frame_strokes[rel_f]:
            for pt in stroke["points"]:
                all_pts.append(pt)
                all_lbls.append(stroke["label"])

        if not all_pts:
            return

        try:
            with torch.inference_mode(), self.amp():
                _, _, logits = self.predictor.add_new_points_or_box(
                    inference_state=self.inference_state,
                    frame_idx=rel_f,
                    obj_id=1,
                    points=np.asarray(all_pts, dtype=np.float32),
                    labels=np.asarray(all_lbls, dtype=np.int32)
                )

            prob = self._convert_logits_to_prob(logits)
            self.tracked_probs[abs_f] = prob
            self.current_raw_prob = self._fullres_prob(prob, self.current_frame_bgr)
            self.lbl_kf_badge.configure(text="★ Keyframe")
            self.render_current_display()
        except Exception as e:
            self.set_status(f"Stroke update error: {e}", persistent=True)

    def clear_current_frame_strokes(self):
        start_f, _ = self._get_range()
        rel_f = self.current_frame_idx - start_f
        if rel_f in self.frame_strokes:
            del self.frame_strokes[rel_f]
        if self.current_frame_idx in self.tracked_probs:
            del self.tracked_probs[self.current_frame_idx]
        self.current_raw_prob = None
        if self.inference_state is not None:
            self.predictor.reset_state(self.inference_state)
            for kf in self.frame_strokes:
                self._update_sam_keyframe(start_f + kf, kf)
        self.lbl_kf_badge.configure(text="")
        self.render_current_display()
        self.set_status(f"Cleared strokes on frame {self.current_frame_idx}.")

    def clear_all_masks(self):
        self.frame_strokes.clear()
        self.tracked_probs.clear()
        self.current_raw_prob = None
        if self.inference_state is not None:
            self.predictor.reset_state(self.inference_state)
        self.lbl_kf_badge.configure(text="")
        self.render_current_display()
        self.set_status("Cleared all masks across the entire clip.")

    # -------------------------------------------------------------
    # MATTE FINESSE PIPELINE (RESOLVE ALPHA LEVELS & REFINEMENT)
    # -------------------------------------------------------------
    def apply_matte_finesse(self, raw_prob):
        if raw_prob is None:
            return None

        if raw_prob.dtype != np.float32:
            prob = raw_prob.astype(np.float32) / 255.0
        else:
            prob = raw_prob.copy()

        # 1. Shrink / Grow (sub-pixel boundary shift in probability space)
        sg_val = float(self.slider_shrink_grow.get())
        if sg_val != 0.0:
            prob = np.clip(prob + (sg_val / 200.0), 0.0, 1.0)

        # 2. Clean Black & Clean White via Alpha Levels
        # Pure black (0) for background, pure white (255) for subject, with a smooth narrow transition
        cb_val = float(self.slider_clean_black.get())  # 0 to 100
        cw_val = float(self.slider_clean_white.get())  # 0 to 100

        black_pt = 0.10 + (cb_val / 100.0) * 0.35
        white_pt = 0.90 - (cw_val / 100.0) * 0.35
        if white_pt <= black_pt + 0.05:
            white_pt = black_pt + 0.05

        alpha_norm = np.clip((prob - black_pt) / (white_pt - black_pt), 0.0, 1.0)
        alpha = (alpha_norm * 255.0).astype(np.uint8)

        # 3. Blur Radius (Feather: narrow soft gradient matching Resolve)
        blur_val = int(self.slider_blur.get())
        if blur_val > 0:
            k = int(blur_val * 0.3) * 2 + 1
            if k >= 3:
                alpha = cv2.GaussianBlur(alpha, (k, k), 0)

        # 4. Invert Mask
        if self.invert_mask:
            alpha = cv2.bitwise_not(alpha)

        return alpha

    # -------------------------------------------------------------
    # TIMELINE & SCRUBBING
    # -------------------------------------------------------------
    def on_slider_scrub(self, val):
        target = int(val)
        if target != self.current_frame_idx:
            self.seek_and_show_frame(target)

    def step_frame(self, delta):
        new_f = max(0, min(self.total_frames - 1, self.current_frame_idx + delta))
        self.time_slider.set(new_f)
        self.seek_and_show_frame(new_f)

    def toggle_play(self):
        self.is_playing = not self.is_playing
        self.btn_play.configure(text="⏸ Pause" if self.is_playing else "▶ Play")
        if self.is_playing:
            threading.Thread(target=self._play_loop, daemon=True).start()

    def _play_loop(self):
        while self.is_playing:
            time.sleep(1.0 / max(10.0, self.fps))
            next_f = (self.current_frame_idx + 1) % max(1, self.total_frames)
            self.after(0, lambda f=next_f: self.time_slider.set(f))
            self.after(0, lambda f=next_f: self.seek_and_show_frame(f))

    def seek_and_show_frame(self, frame_idx):
        self.current_frame_idx = frame_idx
        start_f, _ = self._get_range()
        rel_f = frame_idx - start_f

        temp_img = os.path.join(self.temp_dir, f"{rel_f:05d}.jpg")
        cached_frame = self.ram_frame_cache.get(rel_f)
        if cached_frame is not None:
            frame = cached_frame
            self.current_frame_bgr = cv2.resize(frame, (self.orig_w, self.orig_h))
        elif self.inference_state is not None and os.path.isfile(temp_img):
            frame = cv2.imread(temp_img)
            self.current_frame_bgr = cv2.resize(frame, (self.orig_w, self.orig_h))
        else:
            if self.cap:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ok, frame = self.cap.read()
                if ok:
                    self.current_frame_bgr = frame

        self.current_raw_prob = self._fullres_prob(
            self.tracked_probs.get(frame_idx, None),
            self.current_frame_bgr
        )

        tc = self._format_timecode(frame_idx, self.fps)
        self.lbl_frame_info.configure(text=f"Frame: {frame_idx} / {self.total_frames}  ({tc})")

        has_kf = "★ Keyframe" if rel_f in self.frame_strokes else ""
        self.lbl_kf_badge.configure(text=has_kf)

        self.render_current_display()

    def _format_timecode(self, f, fps):
        total_sec = f / max(1.0, fps)
        hrs = int(total_sec // 3600)
        mins = int((total_sec % 3600) // 60)
        secs = int(total_sec % 60)
        frames = int(f % fps)
        return f"{hrs:02d}:{mins:02d}:{secs:02d}:{frames:02d}"

    # -------------------------------------------------------------
    # RENDERING & DISPLAY
    # -------------------------------------------------------------
    def render_current_display(self):
        if self.current_frame_bgr is None:
            return

        vis = self.current_frame_bgr.copy()
        mask_final = self.apply_matte_finesse(self.current_raw_prob)
        mode = self.preview_mode.get()

        if mask_final is not None:
            if mode == "Overlay (Highlight)":
                # DaVinci Resolve Magic Mask Highlight: Transparent Red overlay
                red_tint = np.zeros_like(vis)
                red_tint[:, :] = (0, 0, 230)
                alpha_norm = (mask_final / 255.0)[..., None]
                vis = (vis * (1.0 - 0.55 * alpha_norm) + red_tint * (0.55 * alpha_norm)).astype(np.uint8)
            elif mode == "Alpha Matte (B&W)":
                vis = cv2.cvtColor(mask_final, cv2.COLOR_GRAY2BGR)
            elif mode == "Output (Cutout)":
                alpha = (mask_final / 255.0)[..., None]
                vis = (vis * alpha).astype(np.uint8)
            elif mode == "Green Screen":
                alpha = (mask_final / 255.0)[..., None]
                bg = np.full_like(vis, (0, 255, 0))
                vis = (vis * alpha + bg * (1 - alpha)).astype(np.uint8)

        shown = cv2.resize(vis, (self.disp_w, self.disp_h))

        # Draw interactive stroke paths on current keyframe
        start_f, _ = self._get_range()
        rel_f = self.current_frame_idx - start_f
        if rel_f in self.frame_strokes:
            for stroke in self.frame_strokes[rel_f]:
                color = (0, 255, 0) if stroke["label"] == 1 else (0, 0, 255)
                pts = stroke.get("canvas_pts", [])
                for i in range(1, len(pts)):
                    cv2.line(shown, pts[i - 1], pts[i], color, 3)

        self.tk_img = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(shown, cv2.COLOR_BGR2RGB)))
        self.canvas.delete("all")
        self.canvas.config(width=self.disp_w, height=self.disp_h)
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)

    # -------------------------------------------------------------
    # TRACKING ENGINE (FORWARD / BACKWARD / BOTH WITH CONSISTENCY)
    # -------------------------------------------------------------
    def start_tracking(self, direction):
        if not self.frame_strokes or self.inference_state is None:
            messagebox.showwarning("Notice", "Draw at least one stroke on an object before tracking.")
            return

        self.stop_tracking_flag = False
        threading.Thread(target=self._tracking_worker, args=(direction,), daemon=True).start()

    def stop_tracking(self):
        self.stop_tracking_flag = True
        self.set_status("Tracking stopped by user.")

    def _tracking_worker(self, direction):
        try:
            self._set_tracking_buttons(False)
            start_f, end_f = self._get_range()
            total_frames = end_f - start_f + 1

            rel_curr = max(0, min(total_frames - 1, self.current_frame_idx - start_f))
            self.set_status(f"Tracking ({direction.capitalize()})...")

            runs = []
            if direction == "both":
                runs.append((rel_curr, True, "Backward"))
                runs.append((rel_curr, False, "Forward"))
            elif direction == "forward":
                runs.append((rel_curr, False, "Forward"))
            elif direction == "backward":
                runs.append((rel_curr, True, "Backward"))

            cons = self.consistency_menu.get()
            strength = 0.0 if cons == "Faster" else (0.25 if cons == "Better" else 0.40)
            ui_interval = max(30, min(90, total_frames // 20 or 1))

            with torch.inference_mode(), self.amp():
                for start_idx, rev, tag in runs:
                    if self.stop_tracking_flag:
                        break

                    for out_idx, _, logits in self.predictor.propagate_in_video(
                        self.inference_state,
                        start_frame_idx=start_idx,
                        reverse=rev
                    ):
                        if self.stop_tracking_flag:
                            break

                        abs_f = start_f + out_idx

                        # High-resolution GPU bilinear interpolation
                        prob_high = self._convert_logits_to_prob(logits)

                        # Motion-Adaptive Temporal Smoothing:
                        # Smooths micro-jitter without ghosting or lagging behind fast motion
                        prev_abs_f = abs_f + 1 if rev else abs_f - 1
                        if strength > 0.0 and prev_abs_f in self.tracked_probs:
                            prev_prob = self.tracked_probs[prev_abs_f]
                            diff = np.abs(prob_high - prev_prob)
                            motion_mask = np.clip(1.0 - (diff / 0.15), 0.0, 1.0)
                            blend_w = motion_mask * strength
                            prob_high = prob_high * (1.0 - blend_w) + prev_prob * blend_w

                        self.tracked_probs[abs_f] = prob_high

                        # Update GUI slider & display periodically
                        if out_idx % ui_interval == 0 or out_idx in (0, total_frames - 1):
                            self.after(0, lambda f=abs_f: self.time_slider.set(f))
                            self.after(0, lambda f=abs_f: self.seek_and_show_frame(f))
                            pct = min(1.0, len(self.tracked_probs) / max(1, total_frames))
                            self.after(0, lambda p=pct: self.progress.set(p))
                            self.set_status(f"Tracking {tag}: Frame {abs_f} ({len(self.tracked_probs)}/{total_frames})")

            if self.device == "cuda":
                torch.cuda.empty_cache()

            self.set_status(f"Tracking complete! Tracked {len(self.tracked_probs)} frames.")
            self.after(0, lambda: self.seek_and_show_frame(self.current_frame_idx))
        except Exception as e:
            self.set_status(f"Tracking error: {e}", persistent=True)
        finally:
            self._set_tracking_buttons(True)

    def _set_tracking_buttons(self, enabled):
        state = "normal" if enabled else "disabled"
        self.after(0, lambda: self.btn_track_both.configure(state=state))
        self.after(0, lambda: self.btn_track_fwd.configure(state=state))
        self.after(0, lambda: self.btn_track_back.configure(state=state))

    # -------------------------------------------------------------
    # MASK EXPORT
    # -------------------------------------------------------------
    def export_mask(self):
        if not self.tracked_probs:
            messagebox.showwarning("Notice", "Track the mask first before exporting.")
            return
        threading.Thread(target=self._export_worker, daemon=True).start()

    def _export_worker(self):
        try:
            self.btn_export.configure(state="disabled")
            start_f, end_f = self._get_range()
            total_render = end_f - start_f + 1

            self.set_status(f"Rendering Alpha Matte ({total_render} frames: {start_f} to {end_f})...")

            source_name = os.path.splitext(os.path.basename(self.video_path))[0]
            output_mask_path = os.path.join(self.output_dir, f"{source_name}_roto_mask.mp4")
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(output_mask_path, fourcc, self.fps, (self.orig_w, self.orig_h))
            source_cap = cv2.VideoCapture(self.video_path)
            source_cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
            for i, f_idx in enumerate(range(start_f, end_f + 1)):
                raw_prob = self.tracked_probs.get(f_idx)
                ok, guide_frame = source_cap.read()
                if raw_prob is not None:
                    fullres_prob = self._fullres_prob(raw_prob, guide_frame if ok else None)
                    refined = self.apply_matte_finesse(fullres_prob)
                else:
                    refined = np.zeros((self.orig_h, self.orig_w), dtype=np.uint8)
                out_frame = cv2.cvtColor(refined, cv2.COLOR_GRAY2BGR)
                writer.write(out_frame)
                if i % 15 == 0 or i == total_render - 1:
                    self.after(0, lambda p=(i + 1) / total_render: self.progress.set(p))
            source_cap.release()
            writer.release()

            self.set_status(f"Exported {total_render} frames: {os.path.basename(output_mask_path)}")
            self.after(0, lambda: messagebox.showinfo(
                "Export Complete",
                f"تم تصدير ماسك كامل:\n{output_mask_path}\n"
                "استورد فيديو الماسك يدويًا داخل DaVinci/Fusion ووصل العقد بنفسك."
            ))
        except Exception as e:
            self.set_status(f"Export error: {e}", persistent=True)
        finally:
            self.after(0, lambda: self.btn_export.configure(state="normal"))


if __name__ == "__main__":
    video = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    if not video or not os.path.isfile(video):
        clip_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "current_clip.txt")
        if os.path.isfile(clip_file):
            try:
                with open(clip_file, "r", encoding="utf-8-sig") as cf:
                    c = cf.read().lstrip("\ufeff").strip()
                if c and os.path.isfile(c):
                    video = c
            except Exception:
                pass
    app = ResolveMagicMaskApp(video)
    app.mainloop()
