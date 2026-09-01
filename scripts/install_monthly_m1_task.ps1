<#
.SYNOPSIS
    M-1（macro_risk_return）探索の専用タスクを Windows タスクスケジューラへ登録する。

.DESCRIPTION
    実体は install_monthly_task.ps1 に委譲する。**登録ロジックを2本に増やさない**ため
    （月次トリガの XML 生成・S4U・窓の検証は片方だけ直す事故が起きやすい）。ここは
    「M-1 タスクの既定値はこれ」を1か所に固定するための薄い入口。

    既定は毎月2日 JST 01:00・16時間の窓。月次本体（1日 01:00）の翌日に置くことで、
    本体（Sigma 863分）と重ならず、翌日の日次バッチ（17:20）の手前で終わる。

    なぜ別タスクなのか: tune:macro_risk_return は実測 2.61分/件 x 288件 = 約752分で
    月次の窓（960分）のほぼ全部を1本で食う。hyperparameter_search は完走してからしか
    永続化しないため、予算内に終わらないステップは時間を使い切って何も残さない（#584）。

    管理者権限（昇格）が必要。LogonType S4U で登録するため、非昇格だと
    Register-ScheduledTask が HRESULT 0x80070005 で落ちる。

.EXAMPLE
    PS> ./scripts/install_monthly_m1_task.ps1
    PS> ./scripts/install_monthly_m1_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Time = "01:00",
    [int]$Day = 2,
    [string]$TaskName = "financial_app-monthly-m1",
    [int]$Hours = 16,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "install_monthly_task.ps1") `
    -Time $Time -Day $Day -TaskName $TaskName -Hours $Hours `
    -Script "run_monthly_m1.ps1" -Unregister:$Unregister
exit $LASTEXITCODE
