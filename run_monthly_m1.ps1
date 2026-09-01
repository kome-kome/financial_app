<#
.SYNOPSIS
    M-1（macro_risk_return）のハイパーパラメータ探索を回す（月次バッチ本体とは別タスク）。

.DESCRIPTION
    2026-09-01 の月次バッチ初実走で、tune:macro_risk_return は 250分の予算を使い切って
    15/288 しか進まなかった。#588 のキャッシュ修正でメモリ枯渇による停止は解消したが、
    所要そのものは変わらない——実測 2.61分/件 x 288件 = 約752分で、月次の窓（16時間）の
    ほぼ全部を1本で食う。

    hyperparameter_search.py の永続化は探索が完走してからしか走らないので、予算内に
    終わらないステップは時間を使い切って何も残さない。そこで「完走見込みのあるステップに
    だけ予算を与える」を原則に、M-1 を別タスク（毎月2日 JST 01:00）へ切り出した（#584）。

    実体は scripts/run_monthly_m1.py にある（骨格は scripts/batch_common.py と共有）。
    ここが薄い起動口に徹しているのは run_monthly.ps1 と同じ理由——PowerShell だと BOM 無しで
    cp932 扱いになって日本語が化ける／python -c へ渡す文字列のダブルクォートが native exe の
    引数で剥がれる、という実行するまで出ない罠を避けるため。

    ログは .logs ディレクトリに monthly_m1_YYYYMMDD.log として残る。
    失敗時は gh issue create で起票する（gh が無くてもバッチ自体は落とさない）。

.PARAMETER DryRun
    実行計画だけ表示して何もしない。

.PARAMETER Steps
    実行するステップをカンマ区切りで限定（deps_smoke / tune:macro_risk_return）。

.PARAMETER NoIssue
    失敗しても Issue を起票しない。

.EXAMPLE
    PS> ./run_monthly_m1.ps1
    PS> ./run_monthly_m1.ps1 -DryRun
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

# 探索ログの日本語が cp932 で落ちないようにする（タスクスケジューラ経由でも同じ）。
$env:PYTHONIOENCODING = "utf-8"

# 正本はローカル。#503 以降は既定も local だが、タスクスケジューラは呼び出し元の環境を
# 引き継ぐことがあるので明示する。
$env:FINAPP_DB_TARGET = "local"

# ローカル読取は Supabase の Egress を1バイトも使わない（run_local.ps1 と同じ理由）。
$env:FINAPP_EGRESS_ENFORCE = "0"
$env:FINAPP_EGRESS_LEDGER  = "0"

$cmd = @("-m", "scripts.run_monthly_m1")
if ($DryRun)  { $cmd += "--dry-run" }
if ($NoIssue) { $cmd += "--no-issue" }
if ($Steps)   { $cmd += @("--steps", $Steps) }

& $py @cmd
exit $LASTEXITCODE
