<#
.SYNOPSIS
    ローカル夜間バッチ（run_nightly.ps1）を Windows タスクスケジューラへ登録する。

.DESCRIPTION
    手順を人の記憶に置かないための登録スクリプト（#503 Phase 2）。**再現可能にしておかないと、
    PC を入れ替えた時点で夜間バッチが黙って消える**——そして失敗が出ないので誰も気づかない。

    既定の起動時刻は JST 17:20。根拠は #476 の確定時刻表:
      東証大引 15:30（2024-11-05 のクロージング・オークション導入後）
      J-Quants 四本値 16:30 頃 / EDINET 受付終了 17:15
    このため 17:20 なら当日ぶんが揃っており、かつ EDINET の当日提出も拾える。

    -StartWhenAvailable（既定オン）で「予定時刻に実行できなかったぶんは次の起動後すぐ」に回す。
    PC がスリープ・停止していた日を翌起動時に追いつかせるためで、**ノート運用ではここが効く**。

.PARAMETER Time
    起動時刻（既定 17:20）。

.PARAMETER TaskName
    タスク名（既定 financial_app-nightly）。

.PARAMETER Unregister
    登録を削除する。

.EXAMPLE
    PS> ./scripts/install_nightly_task.ps1
    PS> ./scripts/install_nightly_task.ps1 -Time 18:00
    PS> ./scripts/install_nightly_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Time = "17:20",
    [string]$TaskName = "financial_app-nightly",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "run_nightly.ps1"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "削除しました: $TaskName" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $script)) {
    Write-Host "run_nightly.ps1 が見つかりません: $script" -ForegroundColor Red
    exit 1
}

# -NoProfile … プロファイル読込で環境が変わるのを避ける（再現性）
# -ExecutionPolicy Bypass … 署名していないローカルスクリプトを走らせる
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# StartWhenAvailable: 見逃した回を次の起動後に実行する（スリープ・停止した日の追いつき）
# DontStopIfGoingOnBatteries / AllowStartIfOnBatteries: ノートでも走らせる
# ExecutionTimeLimit: 差分収集は実測 2h11m 級。6時間で打ち切る（GHA の timeout と同じ考え方）
# MultipleInstances IgnoreNew: 前夜のぶんが走っている間に次が重ならないようにする
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "financial_app ローカル夜間バッチ（#503・正本=ローカルPG）" `
    -Force | Out-Null

Write-Host "登録しました: $TaskName（毎日 $Time）" -ForegroundColor Green
Write-Host "  確認  : Get-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  即実行: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  ログ  : .logs ディレクトリの nightly_YYYYMMDD.log" -ForegroundColor Cyan
