"""
財務分析ツール ランチャー
  - uvicorn サーバーをバックグラウンドで起動し、ブラウザを自動で開く
  - コントロールウィンドウを閉じると同時にサーバーも停止する
  - 既に起動済みの場合はブラウザを開くだけ（既存プロセスはそのまま）
"""
import base64
import os
import struct
import subprocess
import threading
import time
import webbrowser
import zlib
import tkinter as tk
from urllib.request import urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT     = 8000
URL      = f"http://127.0.0.1:{PORT}"
PYTHON   = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")


def _is_running() -> bool:
    try:
        urlopen(URL, timeout=1)
        return True
    except Exception:
        return False


def _start_server():
    log_path = os.path.join(BASE_DIR, "server.log")
    log_file = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "api:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=BASE_DIR,
        stdout=log_file,
        stderr=log_file,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _make_icon_png() -> bytes:
    W, H = 32, 32
    BG   = (30, 41, 59)
    BAR  = (16, 185, 129)
    LINE = (241, 245, 249)

    px = [[list(BG) for _ in range(W)] for _ in range(H)]

    def fill(x1, y1, x2, y2, c):
        for y in range(max(0, y1), min(H, y2 + 1)):
            for x in range(max(0, x1), min(W, x2 + 1)):
                px[y][x] = list(c)

    def draw_line(x0, y0, x1, y1, c):
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            if 0 <= x0 < W and 0 <= y0 < H:
                px[y0][x0] = list(c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy; x0 += sx
            if e2 < dx:
                err += dx; y0 += sy

    bottom, bw, gap, xs = 27, 4, 2, 3
    heights = [12, 18, 14, 22]
    for i, h in enumerate(heights):
        x1 = xs + i * (bw + gap)
        fill(x1, bottom - h + 1, x1 + bw - 1, bottom, BAR)

    cx = [xs + i * (bw + gap) + bw // 2 for i in range(4)]
    cy = [bottom - h for h in heights]
    for i in range(3):
        draw_line(cx[i], cy[i], cx[i + 1], cy[i + 1], LINE)

    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', crc)

    raw = b''.join(b'\x00' + bytes(c for p in row for c in p) for row in px)
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(raw, 9))
        + chunk(b'IEND', b'')
    )


def _make_icon_photo(root) -> tk.PhotoImage:
    return tk.PhotoImage(data=base64.b64encode(_make_icon_png()).decode())


def main():
    already_up = _is_running()
    proc = None if already_up else _start_server()

    # ── ウィンドウ構築 ──────────────────────────────────────────────────
    root = tk.Tk()
    root.title("財務分析ツール")
    root.geometry("300x148")
    root.resizable(False, False)

    # タスクバー・タイトルバーアイコン
    try:
        _icon = _make_icon_photo(root)
        root.iconphoto(True, _icon)
    except Exception:
        pass

    # ヘッダー Canvas
    hdr = tk.Canvas(root, width=300, height=36, bg="#1e293b", highlightthickness=0)
    hdr.pack(fill="x")
    _bar_h = [10, 16, 12, 20]
    _bar_bot, _bw, _gap, _bx = 30, 4, 2, 7
    for i, h in enumerate(_bar_h):
        x1 = _bx + i * (_bw + _gap)
        hdr.create_rectangle(x1, _bar_bot - h + 1, x1 + _bw - 1, _bar_bot,
                              fill="#10b981", outline="")
    _cx = [_bx + i * (_bw + _gap) + _bw // 2 for i in range(4)]
    _cy = [_bar_bot - h for h in _bar_h]
    hdr.create_line(_cx[0], _cy[0], _cx[1], _cy[1], _cx[2], _cy[2], _cx[3], _cy[3],
                    fill="#f1f5f9", width=1.5, smooth=False)
    hdr.create_text(42, 13, text="財務分析ツール", fill="#f1f5f9",
                    font=("", 11, "bold"), anchor="w")
    hdr.create_text(42, 26, text="Japan Equity Financial Analysis",
                    fill="#64748b", font=("", 7), anchor="w")

    status_var = tk.StringVar(value="サーバー起動中...")

    frm = tk.Frame(root, padx=16, pady=10)
    frm.pack(fill="both", expand=True)

    status_lbl = tk.Label(frm, textvariable=status_var, fg="orange",
                          font=("", 10, "bold"), anchor="w")
    status_lbl.grid(row=0, column=0, columnspan=2, sticky="w")

    tk.Label(frm, text=URL, fg="#64748b",
             font=("", 9), anchor="w").grid(row=1, column=0, columnspan=2, sticky="w")

    open_btn = tk.Button(frm, text="ブラウザで開く",
                         command=lambda: webbrowser.open(URL),
                         state="disabled", width=14)
    open_btn.grid(row=2, column=0, pady=(10, 0), sticky="w")

    stop_btn = tk.Button(frm, text="停止して閉じる",  # noqa: F841
                         command=lambda: _shutdown(proc, root),
                         width=14)
    stop_btn.grid(row=2, column=1, pady=(10, 0), padx=(8, 0), sticky="w")

    # ── サーバー起動待ち（別スレッド）──────────────────────────────────
    def _set_ready(label):
        status_var.set(label)
        status_lbl.config(fg="#10b981")
        root.title("財務分析ツール — 稼働中")
        open_btn.config(state="normal")

    def _wait_ready():
        if already_up:
            root.after(0, lambda: _set_ready("● 稼働中（起動済み）"))
            root.after(200, lambda: webbrowser.open(URL))
            return
        for _ in range(120):
            if _is_running():
                root.after(0, lambda: _set_ready("● 稼働中"))
                root.after(200, lambda: webbrowser.open(URL))
                return
            time.sleep(0.5)
        root.after(0, lambda: [
            status_var.set("⚠ 起動失敗 — server.log を確認してください"),
            status_lbl.config(fg="#ef4444"),
        ])

    threading.Thread(target=_wait_ready, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", lambda: _shutdown(proc, root))
    root.mainloop()


def _shutdown(proc, root):
    if proc is not None:
        proc.terminate()
    root.destroy()


if __name__ == "__main__":
    main()
