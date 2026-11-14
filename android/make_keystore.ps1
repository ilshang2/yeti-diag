# 產生 APK 的正式簽章金鑰 (只需要做一次)。
# 金鑰與密碼存在 %USERPROFILE%\.yeti-diag\，不在 repo 裡。這個資料夾請自己另外備份：
# 金鑰遺失或密碼忘記，就再也不能發布可以「覆蓋更新」的新版 APK。
$ErrorActionPreference = "Stop"
$dir = Join-Path $env:USERPROFILE ".yeti-diag"
$store = Join-Path $dir "yeti-release.p12"
$props = Join-Path $dir "keystore.properties"

function Stop-Here($text) {
    Write-Host $text
    Read-Host "按 Enter 關閉視窗" | Out-Null
    exit 1
}

if (Test-Path $store) { Stop-Here "已經有金鑰了：$store (不覆蓋)" }

$keytool = (Get-Command keytool -ErrorAction SilentlyContinue).Source
if (-not $keytool -and $env:JAVA_HOME) { $keytool = Join-Path $env:JAVA_HOME "bin\keytool.exe" }
if (-not $keytool -or -not (Test-Path $keytool)) {
    $jdk = Get-ChildItem (Join-Path $env:USERPROFILE "Tools") -Filter "jdk-*" -Directory -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($jdk) { $keytool = Join-Path $jdk.FullName "bin\keytool.exe" }
}
if (-not $keytool -or -not (Test-Path $keytool)) { Stop-Here "找不到 keytool。請先安裝 JDK 17 或設定 JAVA_HOME。" }

function Read-Plain($prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host "設定 APK 簽章金鑰的密碼 (只用英文字母與數字，至少 8 個字；輸入時不會顯示)。"
$first = Read-Plain "密碼"
$second = Read-Plain "再輸入一次"
if ($first -cne $second) { Stop-Here "兩次輸入不一樣，沒有產生金鑰。請重新執行。" }
if ($first -notmatch '^[A-Za-z0-9]{8,}$') { Stop-Here "密碼要 8 個字以上，只用英文字母與數字。沒有產生金鑰。" }

New-Item -ItemType Directory -Force $dir | Out-Null
& $keytool -genkeypair -storetype PKCS12 -keystore $store -alias yeti -keyalg RSA -keysize 4096 `
    -validity 10000 -storepass $first -keypass $first -dname "CN=yeti-diag"
if ($LASTEXITCODE -ne 0) { Stop-Here "keytool 失敗，沒有產生金鑰。" }

$lines = @(
    "storeFile=$($store -replace '\\', '/')",
    "storePassword=$first",
    "keyAlias=yeti",
    "keyPassword=$first"
)
[IO.File]::WriteAllLines($props, $lines)   # UTF-8 without BOM: Gradle reads it as-is

Write-Host ""
Write-Host "完成。金鑰：$store"
Write-Host "請把 $dir 整個資料夾另外備份一份 (隨身碟或密碼管理員)。"
Read-Host "按 Enter 關閉視窗" | Out-Null
