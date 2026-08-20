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
_CLOSING     = {"flag": False}   # 「停止して閉じる」操作中は停止検知の表示を抑止


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


def _start_server(port: int, target: str = "prod"):
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
        # ブラウザ連動自動停止（ハートビート途絶で自動終了）はランチャー経由のみ有効
        # FINAPP_DB_TARGET は接続先の切替（#481 B-1）。既定は prod＝従来どおり Supabase。
        env={**os.environ, "FINAPP_AUTO_SHUTDOWN": "1", "FINAPP_DB_TARGET": target},
    )
    proc._log_file = log_file  # type: ignore[attr-defined]
    return proc


def _close_proc(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    log_file = getattr(proc, "_log_file", None)
    if log_file is not None:
        try:
            log_file.close()
        except Exception:
            pass


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

    # ── 接続先（#481 B-1・#503 で既定を反転）──────────────────────────────
    # 既定は local＝正本（ローカル PostgreSQL）を見る。**選択は永続化しない**——毎回
    # 正本から始めるほうが、古い断面で起動していることに気づかず使い続ける事故を防げる。
    # 反転前は「毎回 prod から」が同じ理由で正しかった。**どちらが正本かで向きが変わる**
    # ので、ここを触るときは #503 / ADR-0038 を読むこと。prod を選ぶと Supabase の
    # 2026-08-07 断面（更新が止まっている Render 用の窓）を見ることになる。
    # 既定値の源は database.py の `_DEFAULT_TARGET`。ランチャーは database を import
    # しない（engine 生成の副作用を避ける）ので、ここだけ値を写している。
    initial_target = (os.environ.get("FINAPP_DB_TARGET") or "local").strip().lower()
    if initial_target not in ("prod", "local"):
        initial_target = "local"
    # proc は再起動で差し替わるので dict に持つ。gen は「再起動由来の停止」を旧監視スレッドが
    # 誤って『サーバー停止を検知』と表示しないための世代番号。
    state = {"proc": None if already_up else _start_server(port, initial_target), "gen": 0}

    # ── ウィンドウ構築 ──────────────────────────────────────────────────
    root = tk.Tk()
    root.title("財務分析ツール")
    root.geometry("300x214" if hijacked else "300x196")
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

    # 接続先セレクタ。ローカルは「同期時点のデータ」なので、選択中の接続先が常に窓に出続ける
    # ようにしておく（ブラウザ側は common.js のバッジが担当）。
    target_var = tk.StringVar(value=initial_target)
    tgt_frm = tk.Frame(frm)
    tgt_frm.grid(row=3, column=0, columnspan=2, pady=(8, 0), sticky="w")
    tk.Label(tgt_frm, text="接続先", fg="#64748b", font=("", 8)).pack(side="left")
    target_radios = []
    for value, label in (("prod", "本番"), ("local", "ローカル")):
        rb = tk.Radiobutton(tgt_frm, text=label, value=value, variable=target_var,
                            font=("", 9), command=lambda: _switch_target(),
                            state="disabled" if already_up else "normal")
        rb.pack(side="left", padx=(6, 0))
        target_radios.append(rb)
    if already_up:
        tk.Label(tgt_frm, text="(起動済みのため変更不可)", fg="#64748b",
                 font=("", 7)).pack(side="left", padx=(4, 0))

    open_btn = tk.Button(frm, text="ブラウザで開く",
                         command=lambda: webbrowser.open(url),
                         state="disabled", width=14)
    open_btn.grid(row=4, column=0, pady=(10, 0), sticky="w")

    stop_btn = tk.Button(frm, text="停止して閉じる",  # noqa: F841
                         command=lambda: _shutdown(state, root),
                         width=14)
    stop_btn.grid(row=4, column=1, pady=(10, 0), padx=(8, 0), sticky="w")

    def _set_radios(mode):
        if already_up:
            return          # 掌握していないプロセスなので常に無効のまま
        for rb in target_radios:
            rb.config(state=mode)

    # ── サーバー起動待ち（別スレッド）──────────────────────────────────
    def _watch_server_exit(gen: int):
        """サーバー停止（ブラウザ切断の自動停止等）を検知したらランチャーも閉じる。

        gen は世代番号。接続先の切替でプロセスを落としたときは state["gen"] が進むので、
        旧監視スレッドは「停止を検知」と誤表示せずに黙って抜ける。
        """
        p = state["proc"]
        if p is not None:
            p.wait()
        else:
            while _is_running(url):
                time.sleep(5)
        if _CLOSING["flag"] or gen != state["gen"]:
            return
        def _on_exit():
            status_var.set("⏻ サーバー停止を検知 — まもなく閉じます")
            status_lbl.config(fg="#64748b")
            open_btn.config(state="disabled")
            root.after(2500, root.destroy)
        try:
            root.after(0, _on_exit)
        except Exception:
            pass  # ウィンドウ破棄済みなら何もしない

    def _label_for(target: str) -> str:
        return "● 稼働中（ローカルDB）" if target == "local" else "● 稼働中"

    def _set_ready(label):
        status_var.set(label)
        status_lbl.config(fg="#f59e0b" if target_var.get() == "local" else "#10b981")
        root.title("財務分析ツール — 稼働中")
        open_btn.config(state="normal")
        _set_radios("normal")
        threading.Thread(target=_watch_server_exit, args=(state["gen"],), daemon=True).start()

    def _wait_ready(open_browser: bool = True):
        if already_up:
            root.after(0, lambda: _set_ready("● 稼働中（起動済み）"))
            root.after(200, lambda: webbrowser.open(url))
            return
        for _ in range(120):
            if _is_running(url):
                root.after(0, lambda: _set_ready(_label_for(target_var.get())))
                if open_browser:
                    root.after(200, lambda: webbrowser.open(url))
                return
            time.sleep(0.5)
        root.after(0, lambda: [
            status_var.set("⚠ 起動失敗 — logs/server.log を確認してください"),
            status_lbl.config(fg="#ef4444"),
            _set_radios("normal"),
        ])

    def _switch_target():
        """接続先ラジオの変更でサーバーを入れ替える（ブラウザは開き直さない）。

        切替中はラジオを無効化する。連打すると `_work` が多重に走り、後続スレッドが
        直前に起動したばかりのプロセスを掴んで落とす競合になる。
        """
        target = target_var.get()
        status_var.set(f"接続先を切替中… ({'ローカル' if target == 'local' else '本番'})")
        status_lbl.config(fg="orange")
        open_btn.config(state="disabled")
        _set_radios("disabled")

        def _work():
            state["gen"] += 1            # 旧監視スレッドに「これは再起動」と伝える
            _close_proc(state["proc"])
            state["proc"] = _start_server(port, target)
            _wait_ready(open_browser=False)

        threading.Thread(target=_work, daemon=True).start()

    threading.Thread(target=_wait_ready, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", lambda: _shutdown(state, root))
    root.mainloop()


def _shutdown(state, root):
    _CLOSING["flag"] = True
    _close_proc(state.get("proc"))
    root.destroy()


if __name__ == "__main__":
    main()
