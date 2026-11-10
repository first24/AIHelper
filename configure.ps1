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
$assistantGithubToken = Read-PrivateValue 'GitHub fine-grained PAT (hidden)'
$assistantOpenId = Read-Host 'Your Feishu open_id (leave blank to discover later)'

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
    'GITHUB_REPOSITORY=first24/AIHelper',
    'GITHUB_REF_NAME=main',
    ('ALLOWED_OPEN_IDS=' + (ConvertTo-DotEnvValue $assistantOpenId)),
    'ASSISTANT_DB=data/jobs.sqlite3'
)
[IO.File]::WriteAllLines((Join-Path $PSScriptRoot '.env'), $assistantLines, [Text.UTF8Encoding]::new($false))
Write-Host 'Saved .env locally. Never upload this file.'
Write-Host 'Put OPENAI_API_KEY in GitHub repository Actions secrets, not here.'
Write-Host 'Next: python -m assistant discover; send /whoami in Feishu.'
