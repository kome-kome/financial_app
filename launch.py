"""
財務分析ツール ランチャー
  - uvicorn サーバーをバックグラウンドで起動し、ブラウザを自動で開く
  - コントロールウィンドウを閉じると同時にサーバーも停止する
  - 既に起動済みの場合はブラウザを開くだけ（既存プロセスはそのまま）
  - ポート8000を別アプリが占有している場合は 8001〜 の空きポートへ退避して起動
"""
import json
import os
import socket
import subprocess
import threading
import time
import webbrowser
import tkinter as tk
from urllib.error import HTTPError
from urllib.request import urlopen

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8000
PYTHON       = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")
ICON_PATH    = os.path.join(BASE_DIR, "image", "finance_app_icon.png")


def _is_running(url: str) -> bool:
    try:
        urlopen(url, timeout=1)
        return True
    except Exception:
        return False


def _port_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _is_own_server(port: int) -> bool:
    """ポート占有者が本アプリか判定。/health が {"db": ...} を返すのは本アプリのみ。"""
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as res:
            body = res.read()
    except HTTPError as e:
        if e.code != 503:  # /health は DB 断でも 503 + JSON を返す
            return False
        body = e.read()
    except Exception:
        return False
    try:
        return "db" in json.loads(body)
    except Exception:
        return False


def _pick_port() -> int:
    """8001〜8020 から空きポートを返す（全滅なら OS 任せの動的ポート）。"""
    for port in range(DEFAULT_PORT + 1, DEFAULT_PORT + 21):
        if _port_free(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int):
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "server.log")
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "api:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=BASE_DIR,
        stdout=log_file,
        stderr=log_file,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    proc._log_file = log_file  # type: ignore[attr-defined]
    return proc


def _load_icons(root):
    """PNG ファイルから全サイズ＋ヘッダー用サムネイル(28px)を返す。"""
    img = tk.PhotoImage(file=ICON_PATH)
    factor = max(1, min(img.width(), img.height()) // 28)
    thumb = img.subsample(factor, factor) if factor > 1 else img
    return img, thumb


def main():
    # ── ポート決定（別アプリ占有時は空きポートへ退避）──────────────────
    port, already_up, hijacked = DEFAULT_PORT, False, False
    if not _port_free(DEFAULT_PORT):
        if _is_own_server(DEFAULT_PORT):
            already_up = True
        else:
            hijacked = True
            port = _pick_port()
    url = f"http://127.0.0.1:{port}"
    proc = None if already_up else _start_server(port)

    # ── ウィンドウ構築 ──────────────────────────────────────────────────
    root = tk.Tk()
    root.title("財務分析ツール")
    root.geometry("300x166" if hijacked else "300x148")
    root.resizable(False, False)

    # タスクバー・タイトルバーアイコン＋ヘッダーサムネイル
    _icon_full = _icon_thumb = None
    try:
        _icon_full, _icon_thumb = _load_icons(root)
        root.iconphoto(True, _icon_full)
    except Exception:
        pass

    # ヘッダー Canvas
    hdr = tk.Canvas(root, width=300, height=36, bg="#1e293b", highlightthickness=0)
    hdr.pack(fill="x")
    if _icon_thumb:
        hdr.create_image(18, 18, image=_icon_thumb, anchor="center")
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

    tk.Label(frm, text=url, fg="#64748b",
             font=("", 9), anchor="w").grid(row=1, column=0, columnspan=2, sticky="w")

    if hijacked:
        tk.Label(frm, text=f"※ {DEFAULT_PORT}は別アプリ使用中のため {port} で起動",
                 fg="#f59e0b", font=("", 8), anchor="w"
                 ).grid(row=2, column=0, columnspan=2, sticky="w")

    open_btn = tk.Button(frm, text="ブラウザで開く",
                         command=lambda: webbrowser.open(url),
                         state="disabled", width=14)
    open_btn.grid(row=3, column=0, pady=(10, 0), sticky="w")

    stop_btn = tk.Button(frm, text="停止して閉じる",  # noqa: F841
                         command=lambda: _shutdown(proc, root),
                         width=14)
    stop_btn.grid(row=3, column=1, pady=(10, 0), padx=(8, 0), sticky="w")

    # ── サーバー起動待ち（別スレッド）──────────────────────────────────
    def _set_ready(label):
        status_var.set(label)
        status_lbl.config(fg="#10b981")
        root.title("財務分析ツール — 稼働中")
        open_btn.config(state="normal")

    def _wait_ready():
        if already_up:
            root.after(0, lambda: _set_ready("● 稼働中（起動済み）"))
            root.after(200, lambda: webbrowser.open(url))
            return
        for _ in range(120):
            if _is_running(url):
                root.after(0, lambda: _set_ready("● 稼働中"))
                root.after(200, lambda: webbrowser.open(url))
                return
            time.sleep(0.5)
        root.after(0, lambda: [
            status_var.set("⚠ 起動失敗 — logs/server.log を確認してください"),
            status_lbl.config(fg="#ef4444"),
        ])

    threading.Thread(target=_wait_ready, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", lambda: _shutdown(proc, root))
    root.mainloop()


def _shutdown(proc, root):
    if proc is not None:
        proc.terminate()
        log_file = getattr(proc, "_log_file", None)
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass
    root.destroy()


if __name__ == "__main__":
    main()
