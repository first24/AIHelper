$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Read-PrivateValue([string] $Label) {
    $secureValue = Read-Host $Label -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

if (Test-Path -LiteralPath '.env') {
    Write-Host 'An .env file already exists. Open it to update your settings.'
    Start-Process notepad.exe -ArgumentList (Join-Path $PSScriptRoot '.env')
    exit 0
}

$assistantAppId = Read-Host 'Feishu App ID'
$assistantAppSecret = Read-PrivateValue 'Feishu App Secret (hidden)'
$assistantGithubToken = Read-PrivateValue 'GitHub PAT (hidden; leave blank to reuse existing Git login)'
$assistantOpenId = Read-Host 'Your Feishu open_id (leave blank to discover later)'
$assistantDeepseekKey = Read-PrivateValue 'DeepSeek API key for this service (hidden)'
$assistantMailUser = Read-Host 'QQ mailbox address (optional; leave blank to configure later)'
$assistantMailCode = ''
if ($assistantMailUser) {
    $assistantMailCode = Read-PrivateValue 'QQ SMTP client authorization code (hidden; not the QQ password)'
}

# Quote values to avoid dotenv interpolation and preserve special characters.
function ConvertTo-DotEnvValue([string] $Value) {
    if ($Value -match "[\r\n']") {
        throw 'Values must be single-line and cannot contain single quotes.'
    }
    return "'" + $Value + "'"
}

$assistantLines = @(
    ('FEISHU_APP_ID=' + (ConvertTo-DotEnvValue $assistantAppId)),
    ('FEISHU_APP_SECRET=' + (ConvertTo-DotEnvValue $assistantAppSecret)),
    ('GH_TOKEN=' + (ConvertTo-DotEnvValue $assistantGithubToken)),
    'USE_GIT_LOGIN=true',
    'ASSISTANT_MODE=general',
    'ASSISTANT_TIMEZONE=Asia/Shanghai',
    ('DEEPSEEK_API_KEY=' + (ConvertTo-DotEnvValue $assistantDeepseekKey)),
    'DEEPSEEK_MODEL=deepseek-flash',
    'GITHUB_REPOSITORY=first24/AIHelper',
    'GITHUB_REF_NAME=main',
    ('ALLOWED_OPEN_IDS=' + (ConvertTo-DotEnvValue $assistantOpenId)),
    'ASSISTANT_DB=data/jobs.sqlite3',
    'SMTP_HOST=smtp.qq.com',
    'SMTP_PORT=465',
    ('SMTP_USER=' + (ConvertTo-DotEnvValue $assistantMailUser)),
    ('SMTP_AUTH_CODE=' + (ConvertTo-DotEnvValue $assistantMailCode))
)
[IO.File]::WriteAllLines((Join-Path $PSScriptRoot '.env'), $assistantLines, [Text.UTF8Encoding]::new($false))
Write-Host 'Saved .env locally. Never upload this file.'
Write-Host 'The service key is saved locally. Code repair also needs DEEPSEEK_API_KEY in GitHub Actions secrets.'
Write-Host 'Next: python -m assistant discover; send /whoami in Feishu.'
