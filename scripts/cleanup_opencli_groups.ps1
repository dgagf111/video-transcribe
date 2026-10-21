param(
  [switch]$Report
)

# 只读报告 Chrome 里的 OpenCLi Browser 标签页群组。
#
# 纯查询：不抢焦点、不合成任何键盘/鼠标输入、不改动任何窗口。
# 用户 2026-10-20 定：全程后台运行，不允许把窗口切到前台——因此本脚本**只保留查询**，
# 原来的 -Delete（右键菜单「删除群组」）分支已移除。发现残留群组时由调用方只做提示，
# 由用户自己决定怎么处置（右键该 chip →「删除群组」，或重启 Chrome）。
#
# 输出一行：VT_GROUPS_JSON:{"count":N,"names":[...]}
# 判据必须看 Chrome 的标签栏/群组本身：`browser tab list` 会隐藏 about:blank 容器标签页，
# 窗口标题枚举也看不到（它们在既有 Chrome 窗口内），两者都不能用来判断残留。
#
# ⚠️ 两个运行环境约束（2026-10-21 实测，改脚本前先看）：
#   1. **必须用 Windows PowerShell 5.1（powershell.exe）跑，不能用 pwsh 7**。
#      同一段 FindAll(Descendants, Button) 遍历：5.1 约 1.7 秒返回，
#      pwsh 7 直接卡死（45s / 120s 都不返回）。调用方见 vt_common._powershell_exe()。
#   2. **本文件必须存成 UTF-8 with BOM**。5.1 读无 BOM 的 UTF-8 会按 ANSI 解码，
#      中文注释被拆坏后报 `line 37/38: Unexpected token '}'` 并直接解析失败。
#      改完请确认 BOM 还在（EF BB BF 开头）。
#
# chip 的可访问名形如：“OpenCLI Browser”分组 - 已关闭
# 依赖 Chrome 界面语言为中文：英文界面 chip 是 `OpenCLI Browser group`，
# 本正则匹配不到，会一律报告「没有群组」。只影响报告准确性，不影响安全性。

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

# chip 的可访问名形如：“OpenCLI Browser”分组 - 已关闭
# 用 . 吃掉右引号；分组 = \u5206\u7EC4（.NET 正则支持 \uXXXX，避开 PowerShell 的转义问题）
$CHIP = 'OpenCLI Browser.\u5206\u7EC4'

$root = [System.Windows.Automation.AutomationElement]::RootElement

function Get-ChromeWindows {
  $root.FindAll([System.Windows.Automation.TreeScope]::Children,
    (New-Object System.Windows.Automation.PropertyCondition(
       [System.Windows.Automation.AutomationElement]::ClassNameProperty, "Chrome_WidgetWin_1")))
}

function Find-Chips {
  # 逐项输出（不收集成集合再 return）：PowerShell 会 unroll 集合，
  # 空集合 return 后变 $null，下游 @() 会得到「1 个 null 元素」的假结果。
  foreach ($w in (Get-ChromeWindows)) {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
              [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
              [System.Windows.Automation.ControlType]::Button)
    foreach ($b in $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
      try { $n = $b.Current.Name } catch { continue }
      # 区分大小写：真 chip 是 OpenCLI（大写 CLI）；不区分会把聊天/日志文本一起匹配进来
      if ($n -cmatch $CHIP) {
        [pscustomobject]@{ El = $b; Name = $n; Win = $w }
      }
    }
  }
}

function Emit-Result($chips) {
  $names = @($chips | Where-Object { $null -ne $_ } | ForEach-Object { $_.Name })
  $payload = [pscustomobject]@{ count = $names.Count; names = $names } | ConvertTo-Json -Compress
  Write-Output ("VT_GROUPS_JSON:" + $payload)
}

$chips = @(Find-Chips)

if ($chips.Count -eq 0) {
  Write-Output "Chrome 内没有 OpenCLi Browser 群组（只读查询，未动任何窗口）"
} else {
  foreach ($c in $chips) { Write-Output ("发现群组: " + $c.Name) }
  Write-Output "要清掉：右键该群组 →「删除群组」，或重启 Chrome。本 skill 全程后台，不会替你切前台删除。"
}
Emit-Result $chips
exit 0
