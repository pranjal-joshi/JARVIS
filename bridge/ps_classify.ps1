# Classifies the PowerShell command in $env:JARVIS_PS_CMD with PowerShell's own parser, so the device bridge
# can tell a look-only command from one that changes things. Prints one line of JSON:
#   {"error": "..."}                          it doesn't parse
#   {"read": true|false, "block": "...", "why": "..."}
# read: every command is an allow-listed look-only cmdlet or tool, with no redirection and no .NET calls
#       that could change anything; these may run without asking (policy: powershell_read).
# block: the command would write, move or delete files, or hide what it runs (iex, nested shells,
#        encoded commands); the bridge refuses it whatever the policy says.
# Not a sandbox: a command that isn't read-only is shown to you on an approval card before it runs.
# Run through -EncodedCommand by the bridge, so the execution policy for script files doesn't apply.
$ErrorActionPreference = 'Stop'
$src = $env:JARVIS_PS_CMD
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)
if ($errors.Count) { @{ error = $errors[0].Message } | ConvertTo-Json -Compress; exit }

$readVerbs = 'Get','Test','Measure','Select','Where','Sort','Format','Group','Compare','Find','Resolve','Search',
             'ConvertTo','ConvertFrom','Join','Split','Read','Show','Trace'
$readExtra = 'Out-String','Out-Null','Write-Output','ForEach-Object','Where-Object','Select-String'
$readNative = 'whoami','hostname','systeminfo','tasklist','nvidia-smi','netstat','where','ipconfig','driverquery',
              'getmac','nslookup','ping','tracert','query'
$fileCmdlets = 'Remove-Item','Set-Content','Add-Content','Out-File','Clear-Content','Move-Item','Copy-Item',
               'Rename-Item','New-Item','Clear-Item','Set-Item','Remove-ItemProperty','Set-ItemProperty',
               'New-ItemProperty','Rename-ItemProperty','Copy-ItemProperty','Move-ItemProperty','Clear-ItemProperty',
               'Compress-Archive','Expand-Archive','Set-Acl','Export-Csv','Export-Clixml','Export-Alias',
               'Export-FormatData','Export-PSSession','Export-Certificate','Tee-Object','Clear-RecycleBin',
               'Format-Volume','Clear-Disk','Initialize-Disk','Remove-Partition','New-Partition','Resize-Partition',
               'Start-BitsTransfer','Invoke-WebRequest','Invoke-RestMethod','Save-Module','Install-Module',
               'Remove-SmbShare','New-SmbShare'
$opaque = 'Invoke-Expression','Invoke-Command','Start-Job','Start-ThreadJob','Register-ScheduledJob',
          'Register-ScheduledTask','Set-ExecutionPolicy','Add-Type','Import-Module','New-Service','Set-Service',
          'Remove-Service','Enable-PSRemoting','New-PSSession','Enter-PSSession','Set-MpPreference',
          'Add-MpPreference','Disable-WindowsOptionalFeature','Enable-WindowsOptionalFeature','New-LocalUser',
          'Remove-LocalUser','Set-LocalUser','Add-LocalGroupMember'
$fileNative = 'cmd','powershell','pwsh','wscript','cscript','mshta','rundll32','regsvr32','reg','del','erase','rd',
              'rmdir','robocopy','xcopy','copy','move','ren','format','diskpart','takeown','icacls','cacls','cipher',
              'sdelete','attrib','bcdedit','vssadmin','wbadmin','schtasks','sc','net','netsh','bitsadmin','certutil',
              'curl','wget','fsutil','mklink','compact','expand','tar','7z','winget','choco','msiexec','shutdown'
$readMethods = '^(Get|To|Substring|Split|Replace|Trim|TrimStart|TrimEnd|Contains|StartsWith|EndsWith|IndexOf|' +
               'LastIndexOf|Equals|CompareTo|PadLeft|PadRight|Format|Join|Round|Floor|Ceiling|Abs|Max|Min|Add|' +
               'AddDays|AddHours|AddMinutes|AddSeconds|AddMonths|AddYears|Subtract|Parse|TryParse|Exists|Where|' +
               'ForEach|Count|ContainsKey|IsNullOrEmpty|IsNullOrWhiteSpace|ToUpper|ToLower|ToString|Sum|Average)$'
$readTypes = '^(System\.)?(Math|DateTime|DateTimeOffset|TimeSpan|String|Convert|Environment|IO\.Path|Guid|Int32|' +
             'Int64|Double|Decimal|Char|Text\.Encoding|Net\.Dns|TimeZoneInfo|Globalization\.CultureInfo)$'

$read = $true; $block = $null; $why = $null
function Mark-Block($what) { if (-not $script:block) { $script:block = $what } }
function Not-Read($what) { $script:read = $false; if (-not $script:why) { $script:why = $what } }

foreach ($r in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FileRedirectionAst] }, $true)) {
  Mark-Block "writes to a file with $($r.Extent.Text)"
}
foreach ($c in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)) {
  $name = $c.GetCommandName()
  if (-not $name) { Mark-Block 'runs a command whose name is computed, which cannot be checked'; continue }
  $info = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($info -and $info.CommandType -eq 'Alias') {  # rm, del, ls...: judge the cmdlet behind the alias
    $info = Get-Command $info.Definition -ErrorAction SilentlyContinue | Select-Object -First 1
  }
  $full = if ($info) { $info.Name } else { $name }
  $base = [System.IO.Path]::GetFileNameWithoutExtension($full).ToLower()
  $isNative = (-not $info) -or $info.CommandType -eq 'Application'
  if ($isNative) {
    if ($fileNative -contains $base) { Mark-Block "runs $base, which can change files or hide what it runs" }
    elseif ($readNative -contains $base) {
      if ($base -eq 'ipconfig' -and $c.Extent.Text -match '/(release|renew|flushdns|registerdns|setclassid)') {
        Not-Read 'ipconfig changes network settings'
      }
    } else { Not-Read "runs $base" }
    continue
  }
  if ($fileCmdlets -contains $full) { Mark-Block "uses $full, which writes, moves or deletes files"; continue }
  if ($opaque -contains $full) { Mark-Block "uses $full, which can run code that cannot be checked"; continue }
  if ($full -eq 'Start-Process') {
    $target = ($c.CommandElements | Select-Object -Skip 1 | Where-Object {
      $_ -is [System.Management.Automation.Language.StringConstantExpressionAst] } | Select-Object -First 1).Value
    $t = if ($target) { [System.IO.Path]::GetFileNameWithoutExtension($target).ToLower() } else { '' }
    if (-not $target -or $fileNative -contains $t) { Mark-Block 'starts a shell or a program that cannot be checked' }
    if ($c.Extent.Text -match '-Verb\s+RunAs') { Mark-Block 'asks for administrator rights' }
    Not-Read "starts $target"; continue
  }
  $verb = ($full -split '-')[0]
  if (-not ($readVerbs -contains $verb -or $readExtra -contains $full)) { Not-Read "uses $full" }
  if ($full -eq 'Get-Credential') { Not-Read 'asks for a password' }
}
foreach ($m in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
  $member = $m.Member.Extent.Text.Trim("'`"")
  if ($m.Static) {
    $type = $m.Expression.TypeName.FullName
    if ($type -match '^(System\.)?IO\.(File|Directory|FileInfo|DirectoryInfo)$' -and $member -notmatch '^(Exists|Get|Read)') {
      Mark-Block "calls [$type]::$member, which can change files"
    } elseif ($type -match '^(System\.)?(Management\.Automation\.)?ScriptBlock$' -or $type -match 'Reflection|Activator|Marshal') {
      Mark-Block "calls [$type]::$member, which can run code that cannot be checked"
    } elseif (-not ($type -match $readTypes -and $member -match $readMethods) -and $type -notmatch '^(System\.)?IO\.(File|Directory)$') {
      Not-Read "calls [$type]::$member"
    }
  } elseif ($member -match '^(Delete|MoveTo|CopyTo|Encrypt|Decrypt|Create|CreateText|AppendText|Open|OpenWrite|WriteAllText|WriteAllBytes|SetAccessControl)$') {
    Mark-Block "calls .$member(), which can change files"
  } elseif ($member -notmatch $readMethods) {
    Not-Read "calls .$member()"
  }
}
if ($src -match '(?i)-e(nc(odedcommand)?)?\s+[A-Za-z0-9+/=]{16,}') { Mark-Block 'contains an encoded command' }
if ($block) { $read = $false }
@{ read = $read; block = $block; why = $why } | ConvertTo-Json -Compress
