<#
.SYNOPSIS
    バッチ鮮度 watchdog（app_settings の足跡が止まっていないか）を回す。

.DESCRIPTION
    夜間・月次バッチは失敗すれば自分で起票するが、**起動前に死ぬと failure が出ない**
    ので何も届かない（#515・2026-08-21 はログも足跡も 0 バイトだった）。足跡を書く
    仕組みはあったが読む側が無かった。ここがその読む側の起動口。

    実体は scripts/check_batch_freshness.py にある。ここが薄い起動口に徹しているのは
    run_nightly.ps1 と同じ理由——PowerShell だと BOM 無しで cp932 扱いになって日本語が
    化ける／python -c へ渡す文字列のダブルクォートが native exe の引数で剥がれる、という
    **実行するまで出ない**罠が2つあるため。

    タスクスケジューラからは install_watchdog_task.ps1 が登録したタスクが叩く。

.PARAMETER WarnOnly
    停止を検出しても exit 0（誤検知の調査・閾値チューニング用）。

.PARAMETER DryRun
    起票せず gh のコマンド列だけ出す。足跡も残さない。

.PARAMETER Now
    判定時刻を ISO8601 で差し替える（欠落状態の再現・検証用）。

.EXAMPLE
    PS> ./run_watchdog.ps1
    PS> ./run_watchdog.ps1 -WarnOnly
    PS> ./run_watchdog.ps1 -DryRun -Now 2026-08-28T00:00:00+00:00
#>
[CmdletBinding()]
param(
    [switch]$WarnOnly,
    [switch]$DryRun,
    [string]$Now
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
Set-Location $root

$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "venv が見つかりません: $py" -ForegroundColor Red
    exit 1
}

# 出力の日本語が cp932 で落ちないようにする（タスクスケジューラ経由でも同じ）。
$env:PYTHONIOENCODING = "utf-8"

# 正本はローカル。#503 以降は既定も local だが、タスクスケジューラは呼び出し元の環境を
# 引き継ぐことがあるので明示する。
$env:FINAPP_DB_TARGET = "local"

# ローカル読取は Supabase の Egress を1バイトも使わない（run_nightly.ps1 と同じ理由）。
$env:FINAPP_EGRESS_ENFORCE = "0"
$env:FINAPP_EGRESS_LEDGER  = "0"

$cmd = @("-m", "scripts.check_batch_freshness")
if ($WarnOnly) { $cmd += "--warn-only" }
if ($DryRun)   { $cmd += "--dry-run" }
if ($Now)      { $cmd += @("--now", $Now) }

& $py @cmd
exit $LASTEXITCODE
