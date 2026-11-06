# yeti_diag：用藍牙 OBD 接頭診斷 Skoda Yeti (VW PQ35 平台)

**中文** · [English](README_en.md)

用一般的藍牙 ELM327 接頭讀取 Skoda Yeti 各模組：全車故障碼、零件號碼、編碼、測量值、引擎即時數據、
各缸失火、排放自我檢測，並可開關 09 中央電子的隱藏功能。有圖形介面、指令列，輸出也可以給 AI 直接讀
(`--json`、MCP)。

開發與實測的車：**Skoda Yeti 1.2 TSI 2014**，接頭是便宜的 ELM327 v1.5 仿製品與 Vgate V-LINK (v2.1)。
同平台 (Octavia II、Golf 5/6、Superb II…) 可能部分可用，但**沒有測過**，歡迎車友測試回報。

## 從這裡開始

| 你是 | 這樣做 |
|---|---|
| **只想點兩下就用** (Windows) | 到 [Releases](https://github.com/ilshang2/yeti-diag/releases/latest) 下載 `YetiDiag.exe`，見 [圖形介面](#圖形介面) |
| **有 AI 編程助手** (Claude Code、Codex、Gemini CLI…) | 下載這個 repo，在資料夾裡開 AI，貼上 [`PROMPT.md`](PROMPT.md)。AI 會安裝、連車、用白話解釋結果，見 [交給 AI](#交給-ai-agent) |
| **會用指令列** | 見 [安裝](#安裝-windows) 與 [指令列](#指令列快速上手) |
| **想先看看不接車** | 圖形介面的接頭選「SIM」，或指令加 `--sim`，用內建模擬器 |

> ## ⚠️ 使用風險自負
>
> - 這個工具會跟車上的控制模組通訊。**寫入**設錯可能讓功能失效、產生故障碼，最壞會讓模組無法使用。
> - 讀取設計成不會改車，但便宜接頭、電瓶電壓不足仍可能出問題。建議引擎發動或接充電器再用。
> - **寫入功能是實驗性質**，只在作者的一台車上驗證過。不確定就只用讀取。
> - **行駛中只能看「即時儀表」(引擎 OBD)**。車速超過 3 km/h 時程式會拒絕連 ABS 等舊模組與任何寫入：
>   實測行駛中讀 ABS 會讓儀表叮一聲、ABS / 車身穩定燈閃，可能暫停車身穩定功能。
> - 作者不負任何責任 (MIT 授權，見 `LICENSE`)。與 Volkswagen、Škoda、Ross-Tech (VCDS)、Car Scanner 都沒有關係。
> - 不提供：改里程、防盜破解、韌體刷寫、破解安全存取 (Security Access)。

## 目前狀態

| 功能 | 狀態 |
|---|---|
| 全車掃描 `autoscan` (UDS + 舊 TP2.0 模組、故障碼含說明) | 實車通過，結果與 VCDS 相同 |
| 引擎 OBD 即時數據、各缸失火 (Mode $06)、排放自檢 | 實車通過 (含行駛中記錄) |
| 舊模組 (TP2.0 / KWP2000) 故障碼、識別、測量群組 | 實車通過；部分換算公式未驗證 (會標示) |
| 09 隱藏功能讀取 | 實車通過 |
| 09 長編碼寫入 (隱藏功能開關) | **實驗**：實車寫入成功過 (原值寫回)，寫前備份、寫後讀回 |
| UDS 模組寫入 / 清除故障碼 | **實驗**：模擬器通過，實車很少測 |
| 圖形介面 (Windows exe) | 新功能，模擬器通過，實車測試中 |

## 接頭準備

1. 接頭插上車 (方向盤左下方的 OBD 座)，鑰匙轉到 ON (發動最穩)。
2. Windows「藍牙與其他裝置」配對接頭 (PIN 通常是 `1234` 或 `0000`)。
3. 「更多藍牙設定 → COM 連接埠」看方向是「**傳出**」的那個 COM 號碼。

## 圖形介面

**exe (免安裝)**：到 [Releases](https://github.com/ilshang2/yeti-diag/releases/latest) 下載 `YetiDiag.exe`，雙擊執行。
沒有數位簽章，Windows 可能跳「Windows 已保護您的電腦」→ 按「其他資訊」→「仍要執行」。
報告與寫入備份存在 `文件\YetiDiag`。

**從原始碼**：`py -3.13 tools\gui.py` (先做 [安裝](#安裝-windows))。

選接頭的 COM 埠 → 「連線 / 讀電壓」。分頁：

- **即時儀表**：車速、轉速、水溫、電壓、進氣 / 外氣溫度，只讀引擎，行駛中可用，可存檔。
- **健康檢查**：各缸失火、電瓶電壓、排放自檢、引擎 OBD。
- **故障碼**：全車掃描 (要停車，約 1-2 分鐘)。
- **隱藏功能**：讀 09 目前的開關 → 勾選 → 確認新舊值後寫入 (實驗)。第一次會先花約 1 分鐘找模組位址。

## 交給 AI agent

這個工具一開始就是設計給 AI 操作的：每個指令加 `--json` 都回固定格式，也內建 MCP 伺服器 (`.mcp.json`)。
最簡單的玩法 (跟 [cleanyourtv](https://github.com/Gathin23/cleanyourtv) 一樣)：

1. 下載這個 repo (綠色 Code → Download ZIP 解壓，或 `git clone https://github.com/ilshang2/yeti-diag`)。
2. 在資料夾裡開你的 AI 編程助手 (Claude Code、Codex、Gemini CLI…)。
3. 貼上 [`PROMPT.md`](PROMPT.md) 的內容，填車型、接頭、想做什麼，例如「引擎燈亮了，幫我看原因」。

AI 會安裝套件、找接頭、全車掃描，再用白話解釋。PROMPT 裡寫好了規則：行駛中不連舊模組；
**任何寫入都要先給你看新舊值、等你同意才送，只送一次**；不改里程、不破解防盜。
AI 要看的完整說明在 [AI_GUIDE.md](AI_GUIDE.md)。

## 安裝 (Windows)

1. 安裝 [Python 3.13](https://www.python.org/downloads/) (安裝時勾選 *Add python.exe to PATH*)。
2. 下載這個 repo。
3. 在資料夾裡開 PowerShell，安裝套件到 `.deps`：
   ```
   py -3.13 -m pip install -r requirements.txt --target .deps
   ```

## 指令列快速上手

```
.\yeti.ps1 --sim autoscan             # 不接車，用模擬器試跑全車掃描
.\yeti.ps1 --elm COM5 voltage         # 電瓶電壓 (COM5 換成你的埠；auto = 自動找)
.\yeti.ps1 --elm COM5 autoscan        # 全車掃描，存到 reports\
.\yeti.ps1 --elm COM5 obd             # 引擎即時數據與故障碼
.\yeti.ps1 --elm COM5 misfire         # 各缸失火
.\yeti.ps1 --elm COM5 readiness       # 排放自我檢測
.\yeti.ps1 --elm COM5 features        # 09 隱藏功能目前狀態
.\yeti.ps1 --elm COM5 commands        # 所有指令
py -3.13 tools\drive_log.py --elm COM5   # 行駛記錄 (只讀引擎，熄火自動結束)，存到 reports\*_drive.jsonl
```

所有指令加 `--json` 會輸出固定格式的 JSON。完整技術規格見 [規格書.md](規格書.md)。

## 寫入的安全設計

每次寫入都會依序：拒絕黑名單模組 (01 引擎、03 ABS、15 安全氣囊、25 防盜、44 轉向輔助) → 比對零件號碼 →
讀出舊值存成備份 (`backups\`) → 電壓須 ≥ 12.2 V → 車子須靜止 → 顯示新舊值要你確認 →
**只送一次** (失敗不自動重試) → 寫後讀回比對。可以用 `restore` 從備份還原。

## 別的車

模組清單、CAN ID、零件號碼都在 `profiles\skoda_yeti_2014.json`。別的車可以先跑只讀的
`.\yeti.ps1 --elm COM5 discover` 自動產生設定檔，再設環境變數 `YETI_DIAG_PROFILE=檔名`。

## 幫忙測試

1. 先只跑讀取：圖形介面的前三個分頁，或 `voltage`、`autoscan`、`misfire`、`readiness`。
2. 到 [Issues](https://github.com/ilshang2/yeti-diag/issues) 回報：車型 / 年份 / 引擎、接頭型號、哪個功能、結果是否跟 VCDS / Car Scanner 一樣。
3. 貼報告前請**刪掉自己的車身號碼 (VIN，17 碼)**：`reports\` 裡的檔案會記錄從車上讀到的 VIN。

## 開發者

```
$env:PYTHONPATH = ".deps;."; py -3.13 -m pytest tests -q -n auto   # 全部在模擬器上跑，不需接車
```

自己打包 exe：`py -3.13 -m pip install pyinstaller`，再
`py -3.13 -m PyInstaller --onefile --noconsole --name YetiDiag --paths .deps --paths . --add-data "profiles;profiles" --collect-submodules yeti_diag --hidden-import serial --hidden-import serial.tools.list_ports tools\gui.py`。
