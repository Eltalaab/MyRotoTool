import ctypes
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, X, Button, Label, StringVar, Tk, filedialog, messagebox
from tkinter.ttk import Progressbar


APP_NAME = "MyRotoTool"
REPO_ZIP_URL = "https://github.com/Eltalaab/MyRotoTool/archive/refs/heads/main.zip"
SAM2_ZIP_URL = "https://github.com/facebookresearch/sam2/archive/refs/heads/main.zip"
MODEL_URL = "https://media.githubusercontent.com/media/Eltalaab/MyRotoTool/main/sam2_hiera_tiny.pt"
PYTHON_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
PYTHON_SHA256 = ""


def local_app_dir():
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME


def download(url, destination, progress):
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-Installer/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length", 0))
        downloaded = 0
        with open(destination, "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if total:
                    progress(downloaded / total)


def run(command, cwd=None):
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise RuntimeError(f"Command failed ({completed.returncode}):\n{completed.stdout[-4000:]}")
    return completed.stdout


def find_python():
    candidates = [
        local_app_dir() / "python" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python311" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def install_python(work_dir, progress):
    python = find_python()
    if python:
        return python

    installer = work_dir / "python-3.11.9-amd64.exe"
    download(PYTHON_URL, installer, progress)
    if PYTHON_SHA256:
        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        if digest.lower() != PYTHON_SHA256.lower():
            raise RuntimeError("Python installer checksum mismatch.")

    target = local_app_dir() / "python"
    target.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(installer),
            "/quiet",
            "InstallAllUsers=0",
            "PrependPath=0",
            "Include_test=0",
            f"TargetDir={target}",
        ]
    )
    python = target / "python.exe"
    if not python.is_file():
        raise RuntimeError("Python installation completed but python.exe was not found.")
    return python


def extract_zip(archive, destination):
    with zipfile.ZipFile(archive) as source:
        source.extractall(destination)
    roots = list(destination.iterdir())
    if len(roots) == 1 and roots[0].is_dir():
        return roots[0]
    return destination


def create_shortcut(target, shortcut_path):
    command = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$shortcut = $shell.CreateShortcut('{shortcut_path}'); "
        f"$shortcut.TargetPath = '{target}'; "
        f"$shortcut.WorkingDirectory = '{Path(target).parent}'; "
        "$shortcut.Save()"
    )
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command])


class Installer:
    def __init__(self, root):
        self.root = root
        self.root.title("MyRotoTool Setup")
        self.root.geometry("560x230")
        self.root.resizable(False, False)
        self.status = StringVar(value="Ready to install")
        Label(root, text="MyRotoTool", font=("Segoe UI", 18, "bold")).pack(pady=(18, 4))
        Label(root, textvariable=self.status, wraplength=500).pack(pady=5)
        self.progress = Progressbar(root, length=500, mode="determinate")
        self.progress.pack(pady=10)
        self.log_box = Label(root, text="", anchor="w", justify=LEFT, wraplength=500)
        self.log_box.pack(fill=X, padx=28)
        self.start_button = Button(root, text="Install", width=16, command=self.start)
        self.start_button.pack(pady=14)

    def message(self, text):
        self.status.set(text)
        self.root.update_idletasks()

    def set_progress(self, value):
        self.progress["value"] = max(0, min(100, value * 100))
        self.root.update_idletasks()

    def start(self):
        self.start_button.configure(state="disabled")
        threading.Thread(target=self.install, daemon=True).start()

    def install(self):
        try:
            app_dir = local_app_dir()
            app_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="myroto-setup-") as temp:
                work = Path(temp)
                self.message("Downloading Python 3.11...")
                python = install_python(work, self.set_progress)

                venv_dir = app_dir / ".venv"
                if not (venv_dir / "Scripts" / "python.exe").is_file():
                    self.message("Creating the application environment...")
                    run([str(python), "-m", "venv", str(venv_dir)])
                vpython = venv_dir / "Scripts" / "python.exe"

                self.message("Downloading MyRotoTool source...")
                repo_zip = work / "myrototool.zip"
                download(REPO_ZIP_URL, repo_zip, self.set_progress)
                source = extract_zip(repo_zip, work / "source")
                for item in source.iterdir():
                    if item.name not in {".git", ".venv", "roto_storage", "sam2_repo"}:
                        destination = app_dir / item.name
                        if item.is_dir():
                            if destination.exists():
                                shutil.rmtree(destination)
                            shutil.copytree(item, destination)
                        else:
                            shutil.copy2(item, destination)

                self.message("Downloading SAM2 source...")
                sam2_zip = work / "sam2.zip"
                download(SAM2_ZIP_URL, sam2_zip, self.set_progress)
                sam2_source = extract_zip(sam2_zip, work / "sam2")
                self.message("Installing Python packages...")
                run([str(vpython), "-m", "pip", "install", "--upgrade", "pip"])
                requirements = app_dir / "requirements.txt"
                packages = [
                    line.strip()
                    for line in requirements.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("git+")
                ]
                run([str(vpython), "-m", "pip", "install", *packages])
                run([str(vpython), "-m", "pip", "install", str(sam2_source)])

                model = app_dir / "sam2_hiera_tiny.pt"
                if not model.is_file() or model.stat().st_size < 100_000_000:
                    self.message("Downloading the SAM2 model (about 156 MB)...")
                    download(MODEL_URL, model, self.set_progress)

                launcher = app_dir / "Start MyRotoTool.bat"
                launcher.write_text(
                    '@echo off\n'
                    f'cd /d "{app_dir}"\n'
                    f'"{vpython}" roto_app.py %*\n',
                    encoding="utf-8",
                )
                desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
                create_shortcut(launcher, desktop / "MyRotoTool.lnk")

            self.message("Installation complete. Starting MyRotoTool...")
            subprocess.Popen(["cmd", "/c", str(launcher)], cwd=str(app_dir))
            self.root.after(500, self.root.destroy)
        except Exception as error:
            self.root.after(0, lambda: messagebox.showerror("Installation failed", str(error)))
            self.root.after(0, lambda: self.start_button.configure(state="normal"))


if __name__ == "__main__":
    app = Tk()
    Installer(app)
    app.mainloop()
