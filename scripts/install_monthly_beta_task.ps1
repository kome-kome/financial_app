<#
.SYNOPSIS
    macro_beta（M-1 の入力）の専用タスクを Windows タスクスケジューラへ登録する。

.DESCRIPTION
    実体は install_monthly_task.ps1 に委譲する。**登録ロジックを増やさない**ため
    （月次トリガの XML 生成・S4U・窓の検証は片方だけ直す事故が起きやすい）。ここは
    「macro_beta タスクの既定値はこれ」を1か所に固定するための薄い入口。

    既定は毎月2日 JST 01:00・16時間の窓。**月次本体（1日）の翌日・M-1 探索（3日）の前日**
    に置く——macro_beta_loadings は tune:macro_risk_return の入力なので、後ろに置くと
    探索は常に1か月前の loadings を見ることになる（#579）。

    なぜ別タスクなのか: 本番規模の実測は 360分で、月次本体の予算 180分に収まらず、
    本体の窓（960分・Sigma 863 + マージン30）にも増やす空きが無かった。所要が縮む見込みは
    #600 の実測で否定されている（軌道長のレバーは最初から存在しなかった）。

    管理者権限（昇格）が必要。LogonType S4U で登録するため、非昇格だと
    Register-ScheduledTask が HRESULT 0x80070005 で落ちる。

    **登録しただけでは足跡が入らない**（batch_freshness は足跡が無いと「走っていない」と
    判定して起票する）。登録後に1回手動実行すること——初回は -Force で回せば、5週間
    固着している macro_beta_loadings の更新も同時に済む。

.EXAMPLE
    PS> ./scripts/install_monthly_beta_task.ps1
    PS> ./scripts/install_monthly_beta_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Time = "01:00",
    [int]$Day = 2,
    [string]$TaskName = "financial_app-monthly-beta",
    [int]$Hours = 16,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "install_monthly_task.ps1") `
    -Time $Time -Day $Day -TaskName $TaskName -Hours $Hours `
    -Script "run_monthly_beta.ps1" -Unregister:$Unregister
exit $LASTEXITCODE
