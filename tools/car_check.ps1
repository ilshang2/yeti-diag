# 實車只讀檢查：電壓 -> 全車掃描 -> verify 3 次。不會寫入車輛。
# 用法 (專案資料夾): .\tools\car_check.ps1 COM9      (.\tools\car_check.ps1 SIM 1 = 用模擬器測腳本)
param([string]$Port = "COM9", [int]$Rounds = 3)

$root = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$root\.deps;$root"
Set-Location $root

$transport = @(if ($Port -eq "SIM") { "--sim" } else { "--elm"; $Port })  # SIM = 不接車測腳本

function Run-Json([string[]]$cmd) {
    $out = & py -3.13 -m yeti_diag @transport --json @cmd 2>$null
    return [pscustomobject]@{ Exit = $LASTEXITCODE; Doc = ($out -join "`n" | ConvertFrom-Json) }
}

$v = Run-Json @("voltage")
if ($v.Exit -ne 0) { Write-Host "接頭沒有回應: $($v.Doc.error.message)"; exit 3 }
$volt = $v.Doc.data.voltage
Write-Host "電壓 $volt V"
if ($volt -lt 12.0) { Write-Host "電壓低於 12.0 V，先發動引擎或充電再測。"; exit 1 }

Write-Host "`n== 全車掃描 =="
$scan = Run-Json @("autoscan")
foreach ($m in $scan.Doc.data.modules) {
    $n = if ($null -eq $m.dtcs) { "讀不到" } else { "$($m.dtcs.Count) 個故障碼" }
    $err = if ($m.error) { "  ($($m.error))" } else { "" }
    Write-Host ("{0} {1,-10} {2}{3}" -f $m.address, $m.name, $n, $err)
    foreach ($d in $m.dtcs) {
        $text = if ($d.text) { $d.text } else { $d.status_text }
        $state = if ($d.state_text) { "，$($d.state_text)" } else { "" }
        Write-Host "     $($d.code) $text$state"
    }
}
Write-Host "報告: $($scan.Doc.data.report)"

Write-Host "`n== verify x $Rounds =="
$passed = 0
for ($i = 1; $i -le $Rounds; $i++) {
    $r = Run-Json @("verify")
    $fails = ($r.Doc.data.checks | Where-Object { -not $_.ok } | ForEach-Object { "$($_.name): $($_.detail)" }) -join "; "
    Write-Host "第 $i 次: $($r.Doc.data.passed)/$($r.Doc.data.total) $fails"
    if ($r.Exit -eq 0) { $passed++ }
}
Write-Host "`nverify 全過 $passed/$Rounds 次；全車掃描無法讀取: $($scan.Doc.data.unreadable -join '、')"
