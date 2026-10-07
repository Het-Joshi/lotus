"""Local machine integration that works the same on Windows, macOS and Linux."""
import glob
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import pack, tool

pack("system", "open files, URLs and apps, clipboard, notifications, system info")

MAC, WIN = sys.platform == "darwin", os.name == "nt"


def _run(cmd, input_text=None):
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True, errors="replace", timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ""


@tool(pack="system", danger=True)
def open_path(target: str):
    """Open a file, folder or URL with the default application.
    target: path or URL"""
    if WIN:
        os.startfile(target)  # type: ignore[attr-defined]
    elif MAC:
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return f"opened {target}"


@tool(pack="system", danger=True)
def launch_app(name: str, args: str = ""):
    """Start a desktop application by name, e.g. 'Firefox', 'code', 'Spotify'.
    name: application name or executable
    args: optional arguments"""
    extra = shlex.split(args) if args else []
    if MAC:
        subprocess.Popen(["open", "-a", name] + (["--args", *extra] if extra else []))
    elif WIN:
        subprocess.Popen(f'start "" "{name}" {args}', shell=True)
    else:
        exe = shutil.which(name) or shutil.which(name.lower())
        if exe:
            subprocess.Popen([exe, *extra], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        elif shutil.which("gtk-launch"):
            subprocess.Popen(["gtk-launch", name.lower()], start_new_session=True)
        else:
            return f"error: could not find an app called {name}; try list_apps"
    return f"launched {name}"


@tool(pack="system")
def list_apps(filter: str = ""):
    """List installed desktop applications.
    filter: optional substring to match"""
    names = set()
    if MAC:
        for d in ("/Applications", "/System/Applications", str(Path.home() / "Applications")):
            names |= {Path(p).stem for p in glob.glob(d + "/*.app")}
    elif WIN:
        for base in (os.environ.get("APPDATA", ""), os.environ.get("PROGRAMDATA", "")):
            if base:
                names |= {Path(p).stem for p in glob.glob(base + r"\Microsoft\Windows\Start Menu\Programs\**\*.lnk", recursive=True)}
    else:
        for d in ("/usr/share/applications", str(Path.home() / ".local/share/applications"), "/var/lib/flatpak/exports/share/applications"):
            for f in glob.glob(d + "/*.desktop"):
                try:
                    for line in open(f, encoding="utf-8", errors="replace"):
                        if line.startswith("Name="):
                            names.add(line[5:].strip())
                            break
                except OSError:
                    pass
    hits = sorted(n for n in names if filter.lower() in n.lower())
    return ", ".join(hits[:120]) or "no apps found"


@tool(pack="system")
def clipboard_get():
    """Read the clipboard text."""
    if MAC:
        return _run(["pbpaste"])
    if WIN:
        return _run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"])
    for cmd in (["wl-paste"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-ob"]):
        if shutil.which(cmd[0]):
            return _run(cmd)
    return "error: no clipboard tool found (install wl-clipboard or xclip)"


@tool(pack="system", danger=True)
def clipboard_set(text: str):
    """Put text on the clipboard.
    text: text to copy"""
    if MAC:
        cmd = ["pbcopy"]
    elif WIN:
        cmd = ["clip"]
    else:
        cmd = next((c for c in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]) if shutil.which(c[0])), None)
        if not cmd:
            return "error: no clipboard tool found"
    subprocess.run(cmd, input=text, text=True, timeout=10)
    return f"copied {len(text)} chars"


@tool(pack="system")
def notify(title: str, message: str):
    """Show a desktop notification. Useful at the end of long or scheduled tasks.
    title: notification title
    message: notification body"""
    if MAC:
        t, m = title.replace('"', "'"), message.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{m}" with title "{t}"'], timeout=10)
    elif WIN:
        ps = ("[void][Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
              "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
              f"$n.Visible=$true;$n.ShowBalloonTip(5000,'{title.replace(chr(39), '')}','{message.replace(chr(39), '')}','Info');Start-Sleep 6")
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], timeout=10)
    else:
        sys.stdout.write("\a")
        return f"no notifier available; rang the bell. {title}: {message}"
    return "notification shown"


@tool(pack="system")
def system_info(_ctx=None):
    """OS, CPU, memory, disk and which Ollama models are loaded."""
    lines = [f"os: {platform.system()} {platform.release()} ({platform.machine()})",
             f"python: {platform.python_version()}", f"cpus: {os.cpu_count()}"]
    try:
        if Path("/proc/meminfo").exists():
            mi = dict(l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines() if ":" in l)
            lines.append(f"memory: {int(mi['MemAvailable'].split()[0]) // 1024} MB free of {int(mi['MemTotal'].split()[0]) // 1024} MB")
        elif MAC:
            lines.append(f"memory: {int(_run(['sysctl', '-n', 'hw.memsize']) or 0) // 2**20} MB total")
    except Exception:
        pass
    du = shutil.disk_usage(_ctx.cwd if _ctx else ".")
    lines.append(f"disk: {du.free // 2**30} GB free of {du.total // 2**30} GB")
    if _ctx:
        try:
            for m in _ctx.client.loaded():
                lines.append(f"ollama loaded: {m.get('name')} ({m.get('size_vram', 0) // 2**20} MB VRAM, ctx {m.get('context_length', '?')})")
        except Exception:
            pass
    return "\n".join(lines)
