"""プロセス常駐メモリと物理メモリの実測。**資源制約を失敗として現すための計測層**。

なぜ要るか
----------
2026-09-01、月次バッチの初実走で `tune:macro_risk_return` が 156分間 heartbeat を出し続けた
まま探索候補を1件も進めなかった。CPU は 39%（i5-8400・6コア）で余っており、空き物理メモリが
0.6GB／プロセスの WorkingSetSize が測るたびに減る（1974MB → 1013MB → 799MB）＝ページアウト
していた。**CPU が余ってメモリが枯れている**という形は、所要だけを記録していたログからは
読み取れなかった。

`scripts/bench_macro_beta.py` は #512 の時点で「この PC の空きメモリは 2GB 前後しかないので
常駐サイズ自体が律速になりうる」と書いていたが、**測っていたのは自プロセスのピークだけ**で
空き物理メモリを残していなかったため、「ローカルは GHA の 6.4倍遅い」という所要だけが Issue に
残り、原因は確定しなかった。所要は環境で変わる量であって、それ単独では何も証明しない。

設計
----
- **ここが唯一の源**。`scripts/bench_macro_beta.peak_rss_mb` と `scripts/batch_common` の
  heartbeat、`plugins/macro_snapshots` のキャッシュ上限導出はすべてここを呼ぶ（書き写さない）。
- **取れなければ None を返し、例外を投げない**。計測は本業ではないので、計測の失敗が
  バッチを止めてはいけない。呼ぶ側は None を「不明」として扱う。
- 依存を増やさない（psutil は未導入で、本番 `requirements.txt` の footprint を増やす）。
  Windows は ctypes 経由の Win32 API、Linux は `/proc` を読む。
- `pid` を省略すると自プロセス。**子プロセスを測れる**のが要件——バッチの heartbeat が
  測りたいのは自分ではなく重い子。Windows では `pid` から `OpenProcess` するより
  **`Popen` が既に持っているハンドルを渡す方が確実**（`handle=` 引数）: pid 経由は
  別セッション・別整合性レベルのプロセスに対して拒否されうるが、自分が起動した子の
  ハンドルは常に有効で、しかも **pid の再利用による取り違えが原理的に起きない**。
  対話セッションからセッション0（S4U で走るバッチ）のプロセスを pid で測ろうとすると
  実際に拒否される＝「本番でだけ測れない」形になり、今回の一連の失敗と同型になる。
"""
from __future__ import annotations

import sys

__all__ = ["rss_mb", "peak_rss_mb", "available_mb", "total_mb", "snapshot"]

_MB = 1024.0 * 1024.0

# Win32 のアクセス権。PROCESS_QUERY_LIMITED_INFORMATION(0x1000) は Vista 以降で
# GetProcessMemoryInfo に足りる。他ユーザー/より高い整合性レベルのプロセスでは
# PROCESS_QUERY_INFORMATION が拒否されうるので、緩い方から順に試す。
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010


def _win_memory_counters(pid: int | None, handle: int | None = None) -> tuple[float, float] | None:
    """(WorkingSetSize, PeakWorkingSetSize) を MB で返す。取れなければ None。

    `handle` があれば `OpenProcess` を経由しない（`Popen._handle` を渡す想定）。
    このハンドルは呼び出し側の所有物なので **ここでは閉じない**。
    """
    import ctypes
    from ctypes import wintypes

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    # argtypes を明示する。**省略すると 64bit のハンドルが int へ切り詰められて無効化する**
    # （擬似ハンドル -1 を返す GetCurrentProcess で顕在化する）。
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    opened = None
    if handle is not None:
        target = wintypes.HANDLE(int(handle))
    elif pid is None:
        target = kernel32.GetCurrentProcess()
    else:
        for access in (_PROCESS_QUERY_LIMITED_INFORMATION,
                       _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ):
            opened = kernel32.OpenProcess(access, False, int(pid))
            if opened:
                break
        if not opened:
            return None
        target = opened

    try:
        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        if not psapi.GetProcessMemoryInfo(target, ctypes.byref(counters), counters.cb):
            return None
        return (float(counters.WorkingSetSize) / _MB,
                float(counters.PeakWorkingSetSize) / _MB)
    finally:
        if opened:
            kernel32.CloseHandle(opened)


def _win_global_memory() -> tuple[float, float] | None:
    """(AvailPhys, TotalPhys) を MB で返す。取れなければ None。"""
    import ctypes
    from ctypes import wintypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return float(status.ullAvailPhys) / _MB, float(status.ullTotalPhys) / _MB


def _win_process_tree(root_pid: int) -> list[int]:
    """`root_pid` とその子孫の pid。取れなければ `[root_pid]`。

    **これが無いと重い子を測り損ねる**——`venv\\Scripts\\python.exe` はランチャースタブで、
    実体はベース Python の**孫**として動く。`Popen` が持つ pid／ハンドルはスタブのもので、
    そこを測ると常駐 4MB・フォールト千件台という「静かに正しく見える」値が返る（実体は GB 級）。
    同じ性質は `taskkill /F /T` がツリーごと落とす必要がある理由（#532）と同じ根。
    """
    import ctypes
    from ctypes import wintypes

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    snapshot_handle = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot_handle or snapshot_handle == _INVALID_HANDLE_VALUE:
        return [root_pid]

    children: dict[int, list[int]] = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot_handle, ctypes.byref(entry))
        while ok:
            children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot_handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot_handle)

    # 幅優先で子孫を集める。**訪問済みを持つ**——pid は再利用されるので、崩れたスナップショット
    # （親子が循環して見える）で無限ループしないようにする。
    collected: list[int] = []
    seen = {root_pid}
    queue = [root_pid]
    while queue:
        current = queue.pop(0)
        collected.append(current)
        for child in children.get(current, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return collected


def _proc_status_kb(pid: int | None, key: str) -> float | None:
    """Linux `/proc/<pid>/status` から `key:` の値[kB] を引く。"""
    target = "self" if pid is None else str(int(pid))
    try:
        with open(f"/proc/{target}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _proc_meminfo_kb(key: str) -> float | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def rss_mb(pid: int | None = None, handle: int | None = None) -> float | None:
    """現在の常駐メモリ[MB]。`pid` 省略で自プロセス。取れなければ None。

    `handle` は Windows 専用（`Popen._handle`）。他プラットフォームでは無視して `pid` を使う。
    """
    try:
        if sys.platform == "win32":
            counters = _win_memory_counters(pid, handle)
            return None if counters is None else counters[0]
        kb = _proc_status_kb(pid, "VmRSS")
        return None if kb is None else kb / 1024.0
    except Exception:
        return None


def peak_rss_mb(pid: int | None = None, handle: int | None = None) -> float | None:
    """ピーク常駐メモリ[MB]。`pid` 省略で自プロセス。取れなければ None。

    本番規模の posterior は `beta` / `beta_raw`（n_stock x n_factor）が draws x chains ぶん
    保存されて GB 級になる。所要とは別に常駐サイズ自体が律速になりうるので併せて残す（#512）。
    """
    try:
        if sys.platform == "win32":
            counters = _win_memory_counters(pid, handle)
            return None if counters is None else counters[1]
        kb = _proc_status_kb(pid, "VmHWM")
        return None if kb is None else kb / 1024.0
    except Exception:
        return None


def available_mb() -> float | None:
    """空き物理メモリ[MB]。取れなければ None。

    Linux は `MemAvailable`（`MemFree` ではない——ページキャッシュのうち回収可能なぶんを
    含まないと、実際には使える量を過小に見積もる）。
    """
    try:
        if sys.platform == "win32":
            got = _win_global_memory()
            return None if got is None else got[0]
        kb = _proc_meminfo_kb("MemAvailable")
        return None if kb is None else kb / 1024.0
    except Exception:
        return None


def total_mb() -> float | None:
    """物理メモリ総量[MB]。取れなければ None。"""
    try:
        if sys.platform == "win32":
            got = _win_global_memory()
            return None if got is None else got[1]
        kb = _proc_meminfo_kb("MemTotal")
        return None if kb is None else kb / 1024.0
    except Exception:
        return None


def _linux_process_tree(root_pid: int) -> list[int]:
    """Linux 版のプロセスツリー。`/proc/<pid>/stat` の4番目のフィールドが ppid。"""
    import os

    children: dict[int, list[int]] = {}
    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return [root_pid]
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                content = fh.read()
            # comm は空白や括弧を含みうるので、**最後の ')' より後ろ**を切ってから split する。
            tail = content[content.rindex(")") + 1:].split()
            children.setdefault(int(tail[1]), []).append(pid)
        except (OSError, ValueError, IndexError):
            continue

    collected: list[int] = []
    seen = {root_pid}
    queue = [root_pid]
    while queue:
        current = queue.pop(0)
        collected.append(current)
        for child in children.get(current, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return collected


def process_tree(root_pid: int) -> list[int]:
    """`root_pid` とその子孫の pid（root を含む）。取れなければ `[root_pid]`。"""
    try:
        if sys.platform == "win32":
            return _win_process_tree(root_pid)
        return _linux_process_tree(root_pid)
    except Exception:
        return [root_pid]


def tree_rss_mb(root_pid: int) -> tuple[float | None, int]:
    """プロセスツリー全体の常駐メモリ[MB]と、測れたプロセス数。

    **合計すべき理由**は `_win_process_tree` の docstring を参照（ランチャースタブ）。
    1件も測れなければ `(None, 0)`——0.0 と None を区別する（「食っていない」と
    「測れなかった」を同一化しない）。
    """
    total = 0.0
    counted = 0
    for pid in process_tree(root_pid):
        value = rss_mb(pid)
        if value is not None:
            total += value
            counted += 1
    return (total, counted) if counted else (None, 0)


def snapshot(pid: int | None = None, handle: int | None = None) -> dict:
    """1行ログ向けの実測一式。値が取れない項目は None のまま残す（欠測を隠さない）。"""
    return {
        "rss_mb": rss_mb(pid, handle),
        "peak_rss_mb": peak_rss_mb(pid, handle),
        "avail_mb": available_mb(),
        "total_mb": total_mb(),
    }


def format_line(root_pid: int | None = None) -> str:
    """heartbeat 等へ差し込む短い表現。**測れなかったことも `?` として出す**（黙って消さない）。

    `root_pid` を渡すとそのプロセスツリーの合計を出す（省略時は自プロセスのみ）。
    出すのは「そのステップがどれだけ食っているか」と「機械にどれだけ残っているか」の2つ
    ——後者が 0 に張り付くとページアウトが始まり、所要は進行を止めたまま伸びる。
    """
    if root_pid is None:
        used, procs = rss_mb(), 1
    else:
        used, procs = tree_rss_mb(root_pid)
    avail, total = available_mb(), total_mb()

    def _fmt(value: float | None) -> str:
        return "?" if value is None else f"{value:.0f}MB"

    proc_note = "" if procs <= 1 else f"({procs}proc)"
    return f"mem rss={_fmt(used)}{proc_note} avail={_fmt(avail)}/{_fmt(total)}"


if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else None
    for key, value in snapshot(target).items():
        print(f"{key:14s} {value if value is None else round(value, 1)}")
