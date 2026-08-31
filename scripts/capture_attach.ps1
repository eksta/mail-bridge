# capture_attach.ps1 — full setup after BIOS VT-x is enabled
# Usage: powershell -File scripts\capture_attach.ps1
$ErrorActionPreference = "Stop"
$sdk = "$env:LOCALAPPDATA\Android\Sdk"
$adb = "$sdk\platform-tools\adb.exe"
# path to the Yandex Mail APK to install
$apk = $env:YANDEX_APK
if (-not $apk -or -not (Test-Path $apk)) {
    Write-Error "set YANDEX_APK env var to the APK path first"
    exit 1
}
$ca = "$env:USERPROFILE\.mitmproxy\c8750f0d.0"
$mitm = Get-ChildItem "$env:LOCALAPPDATA\Python" -Recurse -Filter "mitmdump.exe" |
    Select-Object -First 1 -ExpandProperty FullName
$dump = "$PSScriptRoot\mitm_dump.txt"

# 1) start aehd (elevated, UAC prompt)
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "sc start aehd" -Verb RunAs -Wait
Start-Sleep 2
sc.exe query aehd | Select-String STATE

# 2) start emulator
Start-Process -FilePath "$sdk\emulator\emulator.exe" `
    -ArgumentList "-avd yandex_test -writable-system -no-snapshot -no-boot-anim -gpu auto"
"waiting for device..."
& $adb wait-for-device

# 3) wait for full boot
do {
    Start-Sleep 5
    $boot = (& $adb shell getprop sys.boot_completed 2>$null).Trim()
} while ($boot -ne "1")
"boot completed"

# 4) root + remount + system CA
& $adb root
Start-Sleep 3
& $adb wait-for-device
& $adb remount
Start-Sleep 2
& $adb push $ca /system/etc/security/cacerts/
& $adb shell chmod 644 /system/etc/security/cacerts/c8750f0d.0

# 5) global proxy -> host mitmproxy
& $adb shell settings put global http_proxy 10.0.2.2:8080

# 6) install APK
& $adb install -r $apk

# 7) start capture
Start-Process -FilePath $mitm `
    -ArgumentList "-p 8080", "--set", "flow_detail=3", "-w", "$dump"

# 8) launch Yandex Mail
& $adb shell monkey -p ru.yandex.mail -c android.intent.category.LAUNCHER 1
"READY: log into Yandex Mail in the emulator, open a compose window,"
"attach a file and send. Then tell the assistant - dump is at $dump"
