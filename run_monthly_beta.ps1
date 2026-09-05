<#
.SYNOPSIS
    M-1 の入力 macro_beta_loadings（PyMC/NUTS 階層マクロ・ベータ）を作る（月次本体とは別タスク）。

.DESCRIPTION
    本番規模の実測は 360分（2026-09-05・3,837銘柄・n_obs 95,010・draws=800）。月次本体の
    予算は 180分で、増やそうにも本体の窓（960分）に空きが無かった（Sigma 863 + マージン30）。
    所要が縮む見込みが無いことは #600 で確定している——target_accept を下げる案も
    max_tree_depth を上げる案も実測で棄却され、そもそも「steps/draw が 1023 に張り付いて
    いる」という前提自体が誤りだった（上限を倍にしても歩数も ESS もビット単位で同一）。

    そこで #584（M-1 探索）と同じ形で別タスクへ出した（#579）。起動は毎月2日 JST 01:00 で、
    M-1 探索（3日へ移動）より前でなければならない——macro_beta_loadings は
    tune:macro_risk_return の入力なので、後ろに置くと探索は常に1か月前の値を見る。

    実体は scripts/run_monthly_beta.py にある（骨格は scripts/batch_common.py と共有）。
    ここが薄い起動口に徹しているのは run_monthly.ps1 と同じ理由——PowerShell だと BOM 無しで
    cp932 扱いになって日本語が化ける／python -c へ渡す文字列のダブルクォートが native exe の
    引数で剥がれる、という実行するまで出ない罠を避けるため。

    ログは .logs ディレクトリに monthly_beta_YYYYMMDD.log として残る。
    失敗時は gh issue create で起票する（gh が無くてもバッチ自体は落とさない）。

.PARAMETER DryRun
    実行計画だけ表示して何もしない。

.PARAMETER Steps
    実行するステップをカンマ区切りで限定（deps_smoke / macro_beta）。

.PARAMETER NoIssue
    失敗しても Issue を起票しない。

.PARAMETER Force
    収束ゲート（r_hat_max <= 1.05）を無視して live として persist する。**人手で結果を
    精査したときの1回きりの経路**で、タスク登録側では渡さない。付けない通常実行では、
    ゲートに落ちた run は status=quarantined で保全され exit は非0になる（#609）。

.EXAMPLE
    PS> ./run_monthly_beta.ps1
    PS> ./run_monthly_beta.ps1 -DryRun
    PS> ./run_monthly_beta.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$Steps,
    [switch]$NoIssue,
    [switch]$Force
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

$cmd = @("-m", "scripts.run_monthly_beta")
if ($DryRun)  { $cmd += "--dry-run" }
if ($NoIssue) { $cmd += "--no-issue" }
if ($Force)   { $cmd += "--force" }
if ($Steps)   { $cmd += @("--steps", $Steps) }

& $py @cmd
exit $LASTEXITCODE
