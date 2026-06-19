cd "C:\Users\Alexandr\Desktop\Interprex"
$env:TAURI_SIGNING_PRIVATE_KEY = [System.Environment]::GetEnvironmentVariable('TAURI_SIGNING_PRIVATE_KEY', 'User')
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = ''
Write-Host "KEY len: $($env:TAURI_SIGNING_PRIVATE_KEY.Length)"
Write-Host "PASS set: '$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD'"
Write-Host "Running: npm run tauri build"
npm run tauri build 2>&1
