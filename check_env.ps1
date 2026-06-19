$k = [System.Environment]::GetEnvironmentVariable('TAURI_SIGNING_PRIVATE_KEY', 'User')
if ($k) { Write-Host "KEY_EXISTS: $($k.Length) chars" } else { Write-Host "KEY_MISSING" }
$p = [System.Environment]::GetEnvironmentVariable('TAURI_SIGNING_PRIVATE_KEY_PASSWORD', 'User')
if ($null -ne $p) { Write-Host "PASS_EXISTS: $($p.Length) chars" } else { Write-Host "PASS_MISSING" }
