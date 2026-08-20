<#
.SYNOPSIS
    ローカル月次バッチ（Fama-MacBeth 重み → M-1 マクロベータ推論 → M-1/M-2/M-3 探索）を回す。

.DESCRIPTION
    #503 で正本がローカル PostgreSQL へ移り GHA の cron を全て止めたとき、日次だけを
    ローカルへ移して月次3本は止まったままだった（#504）。ここがその駆動口。

    止まっていても failure は出ない——無実行は成功でも失敗でもないので notify-failure でも
    macro-health でも拾えない。だから「走らなかった」ことは app_settings の足跡
    （monthly_last_run / monthly_last_success）で見る。

    実体は scripts/run_monthly.py にある（骨格は scripts/batch_common.py と共有）。
    ここが薄い起動口に徹しているのは run_nightly.ps1 と同じ理由——PowerShell だと BOM 無しで
    cp932 扱いになって日本語が化ける／python -c へ渡す文字列のダブルクォートが native exe の
    引数で剥がれる、という**実行するまで出ない**罠を避けるため。

    ステップ間で止まらない。ログは .logs ディレクトリに monthly_YYYYMMDD.log として残る。
    失敗時は gh issue create で起票する（gh が無くてもバッチ自体は落とさない）。

.PARAMETER DryRun
    実行計画だけ表示して何もしない。

.PARAMETER Steps
    実行するステップをカンマ区切りで限定（factor_premia / macro_beta / tune:<model>）。

.PARAMETER NoIssue
    失敗しても Issue を起票しない。

.EXAMPLE
    PS> ./run_monthly.ps1
    PS> ./run_monthly.ps1 -DryRun
    PS> ./run_monthly.ps1 -Steps factor_premia -NoIssue
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

$cmd = @("-m", "scripts.run_monthly")
if ($DryRun)  { $cmd += "--dry-run" }
if ($NoIssue) { $cmd += "--no-issue" }
if ($Steps)   { $cmd += @("--steps", $Steps) }

& $py @cmd
exit $LASTEXITCODE
