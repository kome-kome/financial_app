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

    **管理者権限（昇格）が必要。** LogonType S4U で登録するため、非昇格だと
    Register-ScheduledTask が HRESULT 0x80070005（Access is denied）で落ちる（#515）。

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

# S4U での登録には昇格が要る。非昇格だと Register-ScheduledTask が
# HRESULT 0x80070005（Access is denied）で落ちる（2026-08-25 実測）。ここで止めないと
# CIM の生エラーが出るだけで「何が足りないのか」が読めない。
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "管理者権限で実行してください（LogonType S4U の登録には昇格が必要）" -ForegroundColor Red
    Write-Host "  例: Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-File','$PSCommandPath'" -ForegroundColor Cyan
    exit 1
}

# -NoProfile … プロファイル読込で環境が変わるのを避ける（再現性）
# -ExecutionPolicy Bypass … 署名していないローカルスクリプトを走らせる
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# S4U（Service For User）… 既定の InteractiveToken だと対話セッションに紐づくため、
# 対話コンソール側の CTRL_C 相当に巻き込まれて 0xC000013A（STATUS_CONTROL_C_EXIT）で
# 即死しうる（#515・2026-08-21 にログ0バイトで実測）。S4U はセッション0で走り、
# パスワードも保存しない。**副作用として環境が対話セッションと変わる**ので、
# 初回は .logs のログ先頭で venv・作業ディレクトリ・DB 接続先を必ず確認すること。
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

# StartWhenAvailable: 見逃した回を次の起動後に実行する（スリープ・停止した日の追いつき）
# DontStopIfGoingOnBatteries / AllowStartIfOnBatteries: ノートでも走らせる
# ExecutionTimeLimit: 差分収集は実測 2h11m 級。6時間で打ち切る（GHA の timeout と同じ考え方）
# MultipleInstances IgnoreNew: 前夜のぶんが走っている間に次が重ならないようにする
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "financial_app ローカル夜間バッチ（#503・正本=ローカルPG）" `
    -Force | Out-Null

# **登録できたことを確かめてから成功を出す。** ScheduledTasks の cmdlet は失敗しても
# 非終了エラーで返すため、確認せずに Write-Host すると「登録しました」と嘘をつく
# （install_monthly_task.ps1 の実装で実際に嘘をついた）。DB 書き込みを直接クエリで
# 検証するのと同じ話。S4U は「バッチ ジョブとしてログオン」権限を要求するので、
# ここで LogonType を見ないと権限不足に登録時点で気づけない。
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
[xml]$check = Export-ScheduledTask -TaskName $TaskName
$logon = $check.Task.Principals.Principal.LogonType
$swa   = $check.Task.Settings.StartWhenAvailable
# RunLevel は既定（LeastPrivilege）のとき Windows が XML から要素ごと省略する＝**空が正常**。
# -ne "LeastPrivilege" で見ると正しい登録を弾くので、昇格側だけを弾く。ここを見るのは、
# リポジトリがユーザー書き込み可能な場所にあるため、HighestAvailable になると
# 「誰でも書き換えられるスクリプトが毎晩 admin で走る」形になるから。
$runlevel = $check.Task.Principals.Principal.RunLevel
if ($null -eq $info.NextRunTime)      { Write-Host "登録されたが次回実行時刻が無い（トリガ不正）" -ForegroundColor Red; exit 1 }
if ($logon -ne "S4U")                 { Write-Host "LogonType が $logon（期待 S4U）＝対話コンソールに巻き込まれる形のまま" -ForegroundColor Red; exit 1 }
if ($swa -ne "true")                  { Write-Host "StartWhenAvailable が乗っていない＝見逃した日を追いつけない" -ForegroundColor Red; exit 1 }
if ($runlevel -eq "HighestAvailable") { Write-Host "RunLevel が HighestAvailable＝夜間バッチが管理者権限で走る形になっている" -ForegroundColor Red; exit 1 }

Write-Host "登録しました: $TaskName（毎日 $Time・LogonType=S4U）" -ForegroundColor Green
Write-Host "  次回  : $($info.NextRunTime)" -ForegroundColor Cyan
Write-Host "  確認  : Get-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  即実行: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  ログ  : .logs ディレクトリの nightly_YYYYMMDD.log" -ForegroundColor Cyan
