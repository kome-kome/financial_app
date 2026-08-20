<#
.SYNOPSIS
    ローカル夜間バッチ（差分収集 → マクロ収集 → スコア更新）を回す。

.DESCRIPTION
    正本がローカル PostgreSQL へ移り（#503・ADR-0038）、GHA の cron を全て止めた。
    GHA はクラウドで走るのでローカル DB へは書けず、**駆動主体がこちら側へ来た**。
    Windows タスクスケジューラから叩かれるのがこのスクリプト。

    実体は scripts/run_nightly.py にある。ここが薄い起動口に徹しているのは、PowerShell だと
    BOM 無しで cp932 扱いになって日本語が化ける／python -c へ渡す文字列のダブルクォートが
    native exe の引数で剥がれる、という**実行するまで出ない**罠が2つあるため。
    ステップ順・失敗時の扱いは Python 側にあり tests/test_run_nightly.py が縛っている。

    ステップ間で止まらない（収集が落ちてもスコア更新は走る）。
    ログは .logs ディレクトリに nightly_YYYYMMDD.log として日次ローテートで残る。
    失敗時は gh issue create で起票する（gh が無くてもバッチ自体は落とさない）。

.PARAMETER DryRun
    実行計画だけ表示して何もしない。

.PARAMETER Steps
    実行するステップをカンマ区切りで限定（incremental / macro / scores）。

.PARAMETER NoIssue
    失敗しても Issue を起票しない。

.EXAMPLE
    PS> ./run_nightly.ps1
    PS> ./run_nightly.ps1 -DryRun
    PS> ./run_nightly.ps1 -Steps scores -NoIssue
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$Steps,
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

# 収集ログの日本語が cp932 で落ちないようにする（タスクスケジューラ経由でも同じ）。
$env:PYTHONIOENCODING = "utf-8"

# 正本はローカル。#503 以降は既定も local だが、タスクスケジューラは呼び出し元の環境を
# 引き継ぐことがあるので明示する。
$env:FINAPP_DB_TARGET = "local"

# ローカル読取は Supabase の Egress を1バイトも使わない（run_local.ps1 と同じ理由）。
$env:FINAPP_EGRESS_ENFORCE = "0"
$env:FINAPP_EGRESS_LEDGER  = "0"

$cmd = @("-m", "scripts.run_nightly")
if ($DryRun)  { $cmd += "--dry-run" }
if ($NoIssue) { $cmd += "--no-issue" }
if ($Steps)   { $cmd += @("--steps", $Steps) }

& $py @cmd
exit $LASTEXITCODE
