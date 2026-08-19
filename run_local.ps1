<#
.SYNOPSIS
    financial_app をローカル PostgreSQL 読取レプリカ（financial_db）で起動する。

.DESCRIPTION
    Supabase が Egress 超過で restricted になるとアプリごと止まる（2026-07・2026-08 に2回発生）。
    その間も GUI を使えるように、接続先をローカルの読取レプリカへ向けて起動する。

    既定では既存の tkinter ランチャー `launch.py` を **接続先ラジオの初期値をローカルにした状態**で
    起動する（ブラウザ自動オープン・ポート退避・ブラウザ切断で自動停止まで launch.py の機能）。
    launch.py は素で起動すると毎回 `prod` 始まり＝Supabase を叩きにいくため、restricted 中は
    起動直後に固まる。このスクリプトは `FINAPP_DB_TARGET=local` を先に立てて**一度も本番へ触らせない**。

    接続先の切替は database.resolve_database_url() が FINAPP_DB_TARGET で行う（#481 B-1）。
    **環境変数は自プロセスとその子にしか立てない**。.env は書き換えないので、Supabase 復旧後は
    素の `python launch.py` に戻すだけで prod へ復帰する（戻し忘れ事故が起きない）。

    運用方針は「読取専用」。ミラー（scripts/mirror_*.py）は Supabase を正本とする読取レプリカで、
    逆向きに書く経路をコードとして持たない（ADR-0035）。/collection 画面の収集ボタンや
    collector.py をローカルへ向けて回すと、復旧後に正本と内容が分岐して手動整合が必要になる。

.PARAMETER Console
    tkinter ランチャーを使わず、uvicorn をこのコンソールで前面起動する（ログを直接見たいとき）。

.PARAMETER Port
    待受ポート（既定 8000）。`-Console` のときのみ有効（launch.py は自前でポート退避する）。

.PARAMETER NoBrowser
    起動後にブラウザを自動で開かない。`-Console` のときのみ有効。

.PARAMETER Reload
    コード変更で自動リロードする（開発用）。`-Console` のときのみ有効。

.EXAMPLE
    .\run_local.ps1
    .\run_local.ps1 -Console -Port 8010 -NoBrowser
#>
[CmdletBinding()]
param(
    [switch]$Console,
    [int]$Port = 8000,
    [switch]$NoBrowser,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
Set-Location $root

$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "venv が見つかりません: $py" -ForegroundColor Red
    Write-Host "  python -m venv venv" -ForegroundColor Yellow
    Write-Host "  .\venv\Scripts\Activate.ps1; pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

if (-not $Console) {
    foreach ($p in @("Port", "NoBrowser", "Reload")) {
        if ($PSBoundParameters.ContainsKey($p)) {
            Write-Host "-$p は -Console のときだけ効きます（launch.py は自前でポートとブラウザを扱う）。" -ForegroundColor DarkYellow
        }
    }
}

# ── 接続先をローカルへ固定 ────────────────────────────────────────────────
# launch.py はこの値を接続先ラジオの初期値として読み、uvicorn の子プロセスへも引き継ぐ。
$env:FINAPP_DB_TARGET = "local"

# ローカル読取は Supabase の Egress を1バイトも使わない。したがって:
#   ENFORCE=0 … 400MB / 300万行のプロセス予算で GUI が EgressBudgetExceeded に落ちるのを防ぐ
#               （計測と警告は続くので、異常な読み方をすれば stderr に WARN は出る）
#   LEDGER=0  … .egress/ledger.jsonl にローカル読取を混ぜない
#               （scripts.egress_report の集計を Supabase の実測のまま保つ）
# 請求サイクル累計は database._is_local により自動で無効（db_egress.cycle_tracking_enabled）。
$env:FINAPP_EGRESS_ENFORCE = "0"
$env:FINAPP_EGRESS_LEDGER  = "0"

# ── 接続確認 ──────────────────────────────────────────────────────────────
# ここで落としておかないと、サーバーは起動できてしまい画面側で 500 として現れる。
# Python 側のリテラルは**シングルクォートで書く**——PowerShell は native exe へ渡す引数の
# ダブルクォートをエスケープしないため、"..." を含めると剥がれて SyntaxError になる。
Write-Host "ローカルDBへ接続確認中..." -ForegroundColor Cyan

$probe = @'
import sys
import database as d
from sqlalchemy import text
info = d.db_target_info()
if not info['db_is_local']:
    print('NG|' + info['db_label'])
    sys.exit(2)
with d.engine.connect() as c:
    n = c.execute(text('select count(*) from companies')).scalar()
    w = c.execute(text('select max(week_start) from stock_price_weekly')).scalar()
print('OK|{}|{}|{}'.format(info['db_label'], n, w))
'@

$out = & $py -c $probe
if ($LASTEXITCODE -ne 0) {
    Write-Host "ローカルDBへ接続できません（終了コード $LASTEXITCODE）。" -ForegroundColor Red
    Write-Host "  1) PostgreSQL サービスが起動しているか: Get-Service postgresql*" -ForegroundColor Yellow
    Write-Host "  2) 接続先の既定は postgresql://edinet:edinet@localhost:5432/financial_db" -ForegroundColor Yellow
    Write-Host "     別のURLを使うなら DATABASE_URL_LOCAL を設定する" -ForegroundColor Yellow
    Write-Host "  3) 器が無い場合は python -m scripts.setup_local_db で作成する" -ForegroundColor Yellow
    exit 1
}

$line  = ($out | Select-Object -Last 1)
$parts = $line -split '\|'
if ($parts[0] -ne "OK") {
    Write-Host "接続先がローカルではありません: $line" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "接続先 : $($parts[1])" -ForegroundColor Green
Write-Host "データ : companies $($parts[2]) 社 / 週次株価 最新 $($parts[3])" -ForegroundColor Green
Write-Host "注意   : 読取専用で使う（収集の実行は Supabase 復旧後・正本側で）" -ForegroundColor DarkYellow
Write-Host "         stock_price_daily はミラー対象外のため空（日次チャートのみ非表示）" -ForegroundColor DarkYellow
Write-Host ""

# ── 起動 ──────────────────────────────────────────────────────────────────
if (-not $Console) {
    Write-Host "ランチャー起動: launch.py（接続先ラジオはローカル始まり）" -ForegroundColor Cyan
    & $py (Join-Path $root "launch.py")
    exit $LASTEXITCODE
}

$url = "http://127.0.0.1:$Port/"

$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "ポート $Port は既に使用中です。-Port で別のポートを指定してください。" -ForegroundColor Red
    exit 1
}

$job = $null
if (-not $NoBrowser) {
    $job = Start-Job -ScriptBlock {
        param($u)
        for ($i = 0; $i -lt 60; $i++) {
            try {
                Invoke-WebRequest -Uri ($u + "health") -UseBasicParsing -TimeoutSec 2 | Out-Null
                Start-Process $u
                break
            } catch {
                Start-Sleep -Milliseconds 500
            }
        }
    } -ArgumentList $url
}

Write-Host "起動: $url  （停止は Ctrl+C）" -ForegroundColor Cyan

$uvicornArgs = @("-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Reload) { $uvicornArgs += "--reload" }

try {
    & $py @uvicornArgs
} finally {
    if ($job) { Remove-Job -Job $job -Force -ErrorAction SilentlyContinue }
}
