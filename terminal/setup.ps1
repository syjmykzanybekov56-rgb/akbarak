# АкБарак — установка терминала одним запуском (запускается из setup.bat от имени администратора).
# Можно запускать повторно: что уже стоит — пропускается, файлы АкБарак обновляются.

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName Microsoft.VisualBasic
Add-Type -AssemblyName System.Windows.Forms

$Dir = 'C:\akbarak'
$Raw = 'https://raw.githubusercontent.com/syjmykzanybekov56-rgb/akbarak/main/terminal'
$Tmp = Join-Path $env:TEMP 'akbarak-setup'
New-Item -ItemType Directory -Force -Path $Dir, $Tmp | Out-Null
Start-Transcript -Path (Join-Path $Dir 'setup-log.txt') -Append | Out-Null

$script:Problems = @()
function Step($t) { Write-Host ''; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  OK: $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  !! $t" -ForegroundColor Yellow; $script:Problems += $t }

function Get-File($url, $out) {
    for ($i = 1; $i -le 3; $i++) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -TimeoutSec 900
            if ((Test-Path $out) -and (Get-Item $out).Length -gt 0) { return $true }
        } catch { Write-Host "  попытка $i не удалась: $($_.Exception.Message)"; Start-Sleep 5 }
    }
    return $false
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Find-Python {
    $c = @()
    $c += Get-ChildItem 'C:\Program Files\Python3*\python.exe' -ErrorAction SilentlyContinue
    $c += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue
    $c = $c | Sort-Object FullName -Descending
    if ($c) { return $c[0].FullName }
    return $null
}

Write-Host ''
Write-Host '  АкБарак — установка терминала' -ForegroundColor White
Write-Host '  Не закрывайте это окно, пока не появится итог (10–30 минут).'

# ───────────── Вопросы (сразу, чтобы дальше всё шло само) ─────────────
$curId = [Environment]::GetEnvironmentVariable('AKBARAK_TERMINAL', 'Machine')
if (-not $curId) { $curId = 'kgma-1' }
$TermId = [Microsoft.VisualBasic.Interaction]::InputBox(
    "Номер этого терминала (латиницей, без пробелов).`n`nНапример: kgma-1 — главный корпус, kgma-2 — морфокорпус, kgma-3 — стоматология.`n`nУ каждого терминала номер должен быть свой.",
    'АкБарак — номер терминала', $curId)
$TermId = ($TermId -replace '[^A-Za-z0-9_-]', '').ToLower()
if (-not $TermId) { $TermId = $curId }

$keyFile = Join-Path $Dir 'worker_key.txt'
$oldKey = ''
if (Test-Path $keyFile) { $oldKey = (Get-Content $keyFile -Raw).Trim() }
$Key = [Microsoft.VisualBasic.Interaction]::InputBox(
    "Вставьте WORKER_KEY из Railway (Variables).`n`nЕсли он уже был сохранён или на сервере его нет — оставьте как есть.",
    'АкБарак — ключ терминала', $oldKey)
$Key = ($Key -replace '\s', '')

$ans = [System.Windows.Forms.MessageBox]::Show(
    "Запускать экран АкБарак на весь экран при включении терминала?`n`nВНИМАНИЕ: выйти из этого режима можно только кнопками Alt+F4 — нужна клавиатура или AnyDesk с паролем.`n`nЕсли сомневаетесь — нажмите «Нет», включить можно потом, запустив установку ещё раз.",
    'АкБарак — автозапуск экрана', 'YesNo', 'Question', 'Button2')
$KioskAuto = ($ans -eq 'Yes')

# ───────────── 1. Файлы АкБарак ─────────────
Step '1/8 Файлы АкБарак'
foreach ($f in 'print_worker.py', 'photo_sheet.py', 'start_worker.bat', 'start_kiosk.bat', 'mkassa_relay.py', 'start_mbank.bat') {
    if (Get-File "$Raw/$f" (Join-Path $Dir $f)) { Ok $f } else { Warn "Не скачался файл $f" }
}
if ($Key) { [IO.File]::WriteAllText($keyFile, $Key); Ok 'worker_key.txt сохранён' }
[Environment]::SetEnvironmentVariable('AKBARAK_TERMINAL', $TermId, 'Machine')
$env:AKBARAK_TERMINAL = $TermId
Ok "номер терминала: $TermId"

# ───────────── 2. Python ─────────────
Step '2/8 Python'
$Py = Find-Python
if ($Py) { Ok "уже установлен: $Py" } else {
    $inst = Join-Path $Tmp 'python.exe'
    if (Get-File 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' $inst) {
        Write-Host '  устанавливаю (2–5 минут)...'
        Start-Process $inst -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -Wait
        $Py = Find-Python
        if ($Py) { Ok "установлен: $Py" } else { Warn 'Python не установился — скачайте с python.org и поставьте галочку Add Python to PATH' }
    } else { Warn 'Не скачался Python — скачайте с python.org (галочка Add Python to PATH)' }
}
Refresh-Path

# ───────────── 3. Библиотеки Python ─────────────
Step '3/8 Библиотеки Python'
if ($Py) {
    & $Py -m pip install --upgrade pip --quiet --disable-pip-version-check
    & $Py -m pip install requests pywin32 pillow pypdf --quiet --disable-pip-version-check
    & $Py -c "import requests, win32print, PIL, pypdf" 2>$null
    if ($LASTEXITCODE -eq 0) { Ok 'requests, pywin32, pillow, pypdf' } else { Warn 'Библиотеки Python не установились (pip install requests pywin32 pillow pypdf)' }
} else { Warn 'Библиотеки пропущены — нет Python' }

# ───────────── 4. SumatraPDF ─────────────
Step '4/8 SumatraPDF (печать PDF)'
$SumDir = 'C:\Program Files\SumatraPDF'
$sumPaths = @("$SumDir\SumatraPDF.exe", "$env:LOCALAPPDATA\SumatraPDF\SumatraPDF.exe", "${env:ProgramFiles(x86)}\SumatraPDF\SumatraPDF.exe")
if ($sumPaths | Where-Object { Test-Path $_ }) { Ok 'уже установлен' } else {
    $zip = Join-Path $Tmp 'sumatra.zip'
    if (Get-File 'https://www.sumatrapdfreader.org/dl/rel/3.5.2/SumatraPDF-3.5.2-64.zip' $zip) {
        $ex = Join-Path $Tmp 'sumatra'
        Expand-Archive -Path $zip -DestinationPath $ex -Force
        $exe = Get-ChildItem $ex -Filter '*.exe' -Recurse | Select-Object -First 1
        New-Item -ItemType Directory -Force -Path $SumDir | Out-Null
        Copy-Item $exe.FullName "$SumDir\SumatraPDF.exe" -Force
        if (Test-Path "$SumDir\SumatraPDF.exe") { Ok 'установлен' } else { Warn 'SumatraPDF не установился' }
    } else { Warn 'Не скачался SumatraPDF — скачайте с sumatrapdfreader.org' }
}

# ───────────── 5. Google Chrome ─────────────
Step '5/8 Google Chrome (экран терминала)'
$chromePaths = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
if ($chromePaths | Where-Object { Test-Path $_ }) { Ok 'уже установлен' } else {
    $inst = Join-Path $Tmp 'chrome.exe'
    if (Get-File 'https://dl.google.com/chrome/install/latest/chrome_installer.exe' $inst) {
        Write-Host '  устанавливаю...'
        Start-Process $inst -ArgumentList '/silent /install' -Wait
        if ($chromePaths | Where-Object { Test-Path $_ }) { Ok 'установлен' } else { Warn 'Chrome не установился — экран откроется в Edge (тоже работает)' }
    } else { Warn 'Не скачался Chrome — экран откроется в Edge (тоже работает)' }
}

# ───────────── 6. LibreOffice (печать Word-файлов) ─────────────
Step '6/8 LibreOffice (печать DOC/DOCX)'
$hasWord = Test-Path 'Registry::HKEY_CLASSES_ROOT\Word.Application'
$soffice = @("$env:ProgramFiles\LibreOffice\program\soffice.exe", "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe") | Where-Object { Test-Path $_ }
if ($hasWord) { Ok 'есть Microsoft Word — LibreOffice не нужен' }
elseif ($soffice) { Ok 'уже установлен' }
elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host '  устанавливаю через winget (около 350 МБ, 5–15 минут)...'
    winget install --id TheDocumentFoundation.LibreOffice -e --silent --accept-package-agreements --accept-source-agreements
    $soffice = @("$env:ProgramFiles\LibreOffice\program\soffice.exe", "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe") | Where-Object { Test-Path $_ }
    if ($soffice) { Ok 'установлен' } else { Warn 'LibreOffice не установился — без него не печатаются DOC/DOCX (PDF печатается). Скачайте с libreoffice.org' }
} else { Warn 'Нет LibreOffice и Word — DOC/DOCX не будут печататься (PDF печатается). Скачайте LibreOffice с libreoffice.org' }

# ───────────── 7. AnyDesk (удалённый доступ) ─────────────
Step '7/8 AnyDesk (управление терминалом с телефона/ноутбука)'
$adExe = "${env:ProgramFiles(x86)}\AnyDesk\AnyDesk.exe"
if (-not (Test-Path $adExe)) {
    $inst = Join-Path $Tmp 'AnyDesk.exe'
    if (Get-File 'https://download.anydesk.com/AnyDesk.exe' $inst) {
        Start-Process $inst -ArgumentList "--install `"${env:ProgramFiles(x86)}\AnyDesk`" --start-with-win --silent --create-desktop-icon" -Wait
        Start-Sleep 5
    }
}
$AnyId = ''
if (Test-Path $adExe) {
    $AnyId = (& $adExe --get-id 2>$null | Out-String).Trim()
    if ($AnyId) { Ok "установлен, ID: $AnyId" } else { Ok 'установлен (ID видно, если открыть AnyDesk)' }
} else { Warn 'AnyDesk не установился — скачайте с anydesk.com' }

# ───────────── 8. Питание и автозапуск ─────────────
Step '8/8 Питание и автозапуск'
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off
Ok 'сон и выключение экрана отключены'

$sh = New-Object -ComObject WScript.Shell
$startup = [Environment]::GetFolderPath('CommonStartup')
$desktop = [Environment]::GetFolderPath('CommonDesktopDirectory')
function Make-Link($path, $target, $style) {
    $l = $sh.CreateShortcut($path)
    $l.TargetPath = $target
    $l.WorkingDirectory = $Dir
    $l.WindowStyle = $style
    $l.Save()
}
Make-Link (Join-Path $startup 'АкБарак печать.lnk') "$Dir\start_worker.bat" 7
Make-Link (Join-Path $desktop 'АкБарак печать.lnk') "$Dir\start_worker.bat" 1
Make-Link (Join-Path $desktop 'АкБарак экран.lnk') "$Dir\start_kiosk.bat" 1
Ok 'авто-печать запускается при включении; ярлыки на рабочем столе'
$kioskLink = Join-Path $startup 'АкБарак экран.lnk'
if ($KioskAuto) { Make-Link $kioskLink "$Dir\start_kiosk.bat" 1; Ok 'экран запускается при включении' }
elseif (Test-Path $kioskLink) { Remove-Item $kioskLink -Force; Ok 'автозапуск экрана выключен' }
else { Ok 'экран при включении не запускается (ярлык «АкБарак экран» на рабочем столе)' }

# Запускаем авто-печать прямо сейчас (с обновлённым PATH, чтобы нашёлся новый Python)
Refresh-Path
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*print_worker.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process "$Dir\start_worker.bat" -WorkingDirectory $Dir -WindowStyle Minimized
Ok 'авто-печать запущена (свёрнутое окно)'

# ───────────── Итог ─────────────
$msg = "Терминал: $TermId`n"
if ($AnyId) { $msg += "AnyDesk ID: $AnyId`n" }
$msg += "`n"
if ($script:Problems.Count) { $msg += "Нужно доделать вручную:`n- " + ($script:Problems -join "`n- ") }
else { $msg += "Всё установлено.`n`nОсталось: подключить принтер Canon по USB, сделать его принтером по умолчанию и проверить заказ через админку." }
$msg += "`n`nЖурнал: $Dir\setup-log.txt"
Write-Host ''
Write-Host $msg
Stop-Transcript | Out-Null
[System.Windows.Forms.MessageBox]::Show($msg, 'АкБарак — установка завершена', 'OK', 'Information') | Out-Null
