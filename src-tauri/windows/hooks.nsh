; Data Maskit — NSIS 安装钩子
;
; 职责边界（2026-10-19 重写，issue #42）：
;   1. 安装/卸载前终止本安装目录下的进程（不误杀其它实例）；
;   2. 写入中文「应用和功能」显示名；
;   3. 清理更早产品名（LLMShield）遗留的快捷方式与自启项。
;
; 桌面 / 开始菜单快捷方式**一律交给 Tauri 模板**，本文件不再创建：
;   - 模板在 finish 页按「创建桌面快捷方式」复选框决定是否创建桌面图标；
;   - 模板在 $UpdateMode / $NoShortcutMode 下主动跳过（升级时不重复铺设）；
;   - hook 内无条件 CreateShortcut 看不到勾选状态，会造成「取消勾选仍生成图标」
;     与「勾选后多出一个英文名图标」——即 issue #42 的「多创建一个快捷方式」。
;   代价：快捷方式名回到 productName（Maskit）。中文名只能靠自定义 NSIS 模板
;   （nsis.template）实现，那要整份复制并跟踪 1000 行上游模板，收益不抵成本。

; 按安装目录归属终止进程。安装与卸载共用同一段脚本，避免两份逻辑漂移。
;
; 为什么用 -File 而不是 `-Command "..." -- "$INSTDIR"`（issue #42 的报错原文）：
; Windows PowerShell 5.1 的 -Command 会把其后的**所有**参数拼成命令文本，`--` 不是
; 参数分隔符，于是脚本结尾变成 `... } -- C:\Users\...\Maskit`，报「一元运算符"--"
; 后面缺少表达式」；同时 $args[0] 在 -Command 模式下拿不到值。结果是安装与卸载
; **每次**都弹红字，且一个进程都没被杀掉。
!macro MASKIT_KILL_BY_INSTALLROOT
  InitPluginsDir
  Push $R9
  FileOpen $R9 "$PLUGINSDIR\maskit-kill-by-installroot.ps1" w
  FileWrite $R9 "param([Parameter(Mandatory=$$true)][string]$$Root)$\r$\n"
  FileWrite $R9 "$$root = [IO.Path]::GetFullPath($$Root).TrimEnd('\') + '\'$\r$\n"
  FileWrite $R9 "$$all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)$\r$\n"
  FileWrite $R9 "$$ids = New-Object System.Collections.Generic.HashSet[int]$\r$\n"
  FileWrite $R9 "foreach ($$p in $$all) {$\r$\n"
  FileWrite $R9 "  $$path = $$p.ExecutablePath; $$cmd = $$p.CommandLine$\r$\n"
  FileWrite $R9 "  if (($$path -and $$path.StartsWith($$root, [StringComparison]::OrdinalIgnoreCase)) -or ($$cmd -and $$cmd.IndexOf($$root, [StringComparison]::OrdinalIgnoreCase) -ge 0)) { [void]$$ids.Add([int]$$p.ProcessId) }$\r$\n"
  FileWrite $R9 "}$\r$\n"
  FileWrite $R9 "$$changed = $$true$\r$\n"
  FileWrite $R9 "while ($$changed) {$\r$\n"
  FileWrite $R9 "  $$changed = $$false$\r$\n"
  FileWrite $R9 "  foreach ($$p in $$all) {$\r$\n"
  FileWrite $R9 "    if ($$ids.Contains([int]$$p.ParentProcessId) -and -not $$ids.Contains([int]$$p.ProcessId)) { [void]$$ids.Add([int]$$p.ProcessId); $$changed = $$true }$\r$\n"
  FileWrite $R9 "  }$\r$\n"
  FileWrite $R9 "}$\r$\n"
  FileWrite $R9 "foreach ($$id in $$ids) { Stop-Process -Id $$id -Force -ErrorAction SilentlyContinue }$\r$\n"
  FileClose $R9
  Pop $R9
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\maskit-kill-by-installroot.ps1" "$INSTDIR"'
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro MASKIT_KILL_BY_INSTALLROOT
  Sleep 500
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; 先把 ≤0.11.2 遗留的中文名快捷方式摘掉：那是旧 hook 无条件创建的（不认勾选、
  ; 也不认 $UpdateMode）。不清理的话升级后桌面上会同时留着中文名与新模板创建的
  ; 英文名两个图标。本文件已不再创建它们，所以删除是安全的。
  Delete "$DESKTOP\Data Maskit.lnk"
  Delete "$SMPROGRAMS\Data Maskit.lnk"

  ; 更早产品名（LLMShield）遗留的快捷方式
  Delete "$DESKTOP\LLMShield.lnk"
  Delete "$SMPROGRAMS\LLMShield.lnk"

  ; "应用和功能"列表里的显示名改成中文（默认是 productName=Maskit，中文用户搜不到）
  ; SHCTX 由模板按 perMachine/currentUser 设好，跟随安装模式写 HKLM 或 HKCU
  !ifdef UNINSTKEY
    WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "Data Maskit"
  !endif
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro MASKIT_KILL_BY_INSTALLROOT
  Sleep 500

  ; 中文名快捷方式由旧版本创建，模板只认识 productName 名（Maskit.lnk）自己会删
  Delete "$DESKTOP\Data Maskit.lnk"
  Delete "$SMPROGRAMS\Data Maskit.lnk"
  Delete "$DESKTOP\LLMShield.lnk"
  Delete "$SMPROGRAMS\LLMShield.lnk"

  ; 清掉开机自启项（新旧两个值名，与 lib.rs AUTOSTART_VALUE / panel.py AUTOSTART_VALUE_NAME 一致），
  ; 否则卸载后每次开机弹「找不到 Maskit.exe」。
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Maskit"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "LLMShield"
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; 清理安装目录残留。
  ; 引擎是 PyInstaller onedir，运行时会在 resources\engine 下产生安装清单里没有的
  ; 文件（__pycache__、临时解包产物等）。NSIS 只删清单内的文件，于是卸载后
  ; resources\engine 整个目录还留在盘上（实测确认），用户会觉得"卸载没卸干净"。
  RMDir /r "$INSTDIR\resources\engine"
  RMDir /r "$INSTDIR\resources"
  RMDir "$INSTDIR"

  ; 用户数据目录（%APPDATA%\Maskit：配置、词库、事件库、授权凭证）一律不动。
  ; 那是用户自己的数据，卸载程序不等于授权我们替他删数据；重装后还能接着用，
  ; 真要清理由用户自己删——静默删掉别人几个月的日志是不可接受的。
!macroend
