"""端末で列を揃えるための表示幅。`f"{s:<14}"` は**文字数**で数えるので全角混じりだと崩れる。

East Asian Width の **Ambiguous（`A`）は端末依存**で、cp932 のコンソールでは2幅、UTF-8 の
Windows Terminal では1幅に描かれる（`×` `→` `°` などが該当する）。**どちらかが正しいという
ことが無い**ので、幅の解釈は呼び出し側が `ambiguous` で明示する。既定は1幅。

写しを増やさないための置き場（#623 と同型）。ここへ寄せる前は `check_batch_freshness`（`"FWA"`）
と `preset_ic_gate`（`"WF"`）が同じ2行を各自で持ち、**Ambiguous の扱いだけが割れていた**。
列がわずかにずれるだけなので、割れても失敗としては現れない。
"""
from __future__ import annotations

import unicodedata


def display_width(text, *, ambiguous: int = 1) -> int:
    """端末に描かれる幅（半角を1と数える）。`ambiguous=2` で Ambiguous を全角扱いにする。"""
    wide = "WFA" if ambiguous == 2 else "WF"
    return sum(2 if unicodedata.east_asian_width(c) in wide else 1 for c in str(text))


def pad(text, width: int, *, ambiguous: int = 1) -> str:
    """`width` の表示幅になるよう右へ空白を足す（足りていれば何もしない）。"""
    s = str(text)
    return s + " " * max(0, width - display_width(s, ambiguous=ambiguous))
