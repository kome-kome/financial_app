<#
.SYNOPSIS
    正本（ローカル PostgreSQL）のバックアップを取り、Supabase Storage へ置く（週次）。

.DESCRIPTION
    #503 で復元する側は通っている（Storage から17表を落として使い捨てクラスタへ戻し、
    行数一致と画面9本の表示まで確認済み）。しかし取る側は手で叩く CLI のままで、
    どのバッチにも入っていなかった——それを週次の自動実行にするのが #606。

    起動は毎週日曜 JST 21:00（scripts/install_backup_task.ps1）。夜間バッチは 17:20 開始・
    実測約70分で 18:30 頃に終わるので衝突しない。月次のステップにせず独立タスクにしたのは、
    バックアップが「他が全部こけた日にこそ効く」もので、他の処理の遅延に道連れにされては
    困るため（docs/DEPLOYMENT.md の元からの方針）。

    実体は scripts/run_backup.py にある（骨格は scripts/batch_common.py と共有）。
    ここが薄い起動口に徹しているのは run_monthly_beta.ps1 と同じ理由——PowerShell だと
    BOM 無しで cp932 扱いになって日本語が化ける／python -c へ渡す文字列のダブルクォートが
    native exe の引数で剥がれる、という実行するまで出ない罠を避けるため。

    ログは .logs ディレクトリに backup_YYYYMMDD.log として残る。
    失敗時は gh issue create で起票する（gh が無くてもバッチ自体は落とさない）。

    Storage への push には .env の SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY が要る。

.PARAMETER DryRun
    実行計画だけ表示して何もしない。

.PARAMETER Steps
    実行するステップをカンマ区切りで限定（現状は push の1本のみ）。

.PARAMETER NoIssue
    失敗しても Issue を起票しない。

.EXAMPLE
    PS> ./run_backup.ps1
    PS> ./run_backup.ps1 -DryRun
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

# ログの日本語が cp932 で落ちないようにする（タスクスケジューラ経由でも同じ）。
$env:PYTHONIOENCODING = "utf-8"

# バックアップ元は正本＝ローカルであること。リモートを引いて取ったものは「バックアップ」
# ではなく Supabase の自己複製で、正本が失われたときに何の役にも立たない
# （backup_push.guard_source_is_primary が実際に弾く）。
$env:FINAPP_DB_TARGET = "local"

# ローカル読取は Supabase の Egress を1バイトも使わない（run_local.ps1 と同じ理由）。
$env:FINAPP_EGRESS_ENFORCE = "0"
$env:FINAPP_EGRESS_LEDGER  = "0"

$cmd = @("-m", "scripts.run_backup")
if ($DryRun)  { $cmd += "--dry-run" }
if ($NoIssue) { $cmd += "--no-issue" }
if ($Steps)   { $cmd += @("--steps", $Steps) }

& $py @cmd
exit $LASTEXITCODE
