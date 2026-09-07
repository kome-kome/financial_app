<#
.SYNOPSIS
    平日日中の枠で、重い計算をキューから1日1件ずつ進める（#618）。

.DESCRIPTION
    2026-09-07 に macro_beta を手動で回したところ、同一パネル・同一設定・同一コードなのに
    発散が 0 -> 344 回に増え、収束ゲートに落ちて隔離された。9/6 の run との差は、その7時間の
    裏で重いテストを並走させたことしか見当たらない。本番の推論経路にはスレッド固定が無く、
    XLA が使うコア数は実行時の混み具合で変わる。コア数が変われば浮動小数の加算順序が変わり、
    NUTS は初期のごく小さな差が軌道を分岐させるので、発散の有無まで動きうる。

    つまり「重い計算の裏で作業をしない」という運用条件が、結果の再現性に直結している。
    人が会社に居て PC を触らない平日 8:00-16:00 は、その条件が構造的に満たされる唯一の
    時間帯で、そこを専用の枠にした。

    実体は scripts/run_daytime.py にある（骨格は scripts/batch_common.py と共有）。
    ここが薄い起動口に徹しているのは run_monthly_beta.ps1 と同じ理由。

    ログは .logs ディレクトリに daytime_YYYYMMDD.log として残る。

.PARAMETER DryRun
    実行計画だけ表示して何もしない（キューは減らさない）。

.PARAMETER Queue
    キューの中身を表示する。

.PARAMETER Enqueue
    末尾へ積む。カンマ区切りで複数可（beta / tune:macro_gbdt / tune:macro_dlm）。

.PARAMETER ClearQueue
    キューを空にする。

.PARAMETER NoIssue
    失敗しても Issue を起票しない。

.EXAMPLE
    PS> ./run_daytime.ps1 -Queue
    PS> ./run_daytime.ps1 -Enqueue beta
    PS> ./run_daytime.ps1 -Enqueue "beta,tune:macro_dlm"
    PS> ./run_daytime.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Queue,
    [string]$Enqueue,
    [switch]$ClearQueue,
    [switch]$NoIssue
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
Set-Location $root

$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "venv が見つかりません: $py" -ForegroundColor Red
    exit 1
}

# ログの日本語が cp932 で落ちないようにする（タスクスケジューラ経由でも同じ）。
$env:PYTHONIOENCODING = "utf-8"

# 正本はローカル。#503 以降は既定も local だが、タスクスケジューラは呼び出し元の環境を
# 引き継ぐことがあるので明示する。
$env:FINAPP_DB_TARGET = "local"

# ローカル読取は Supabase の Egress を1バイトも使わない（run_local.ps1 と同じ理由）。
$env:FINAPP_EGRESS_ENFORCE = "0"
$env:FINAPP_EGRESS_LEDGER  = "0"

$cmd = @("-m", "scripts.run_daytime")
if ($Queue)      { $cmd += "--queue" }
if ($ClearQueue) { $cmd += "--clear-queue" }
if ($Enqueue)    { $cmd += @("--enqueue", $Enqueue) }
if ($DryRun)     { $cmd += "--dry-run" }
if ($NoIssue)    { $cmd += "--no-issue" }

& $py @cmd
exit $LASTEXITCODE
