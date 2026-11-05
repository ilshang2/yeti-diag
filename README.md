# yeti_diag：用藍牙 OBD 接頭診斷 Skoda Yeti (VW PQ35 平台)

用一般的藍牙 ELM327 接頭讀取 Skoda Yeti 各模組：全車故障碼、零件號碼、編碼、測量值、引擎即時數據、
各缸失火、排放自我檢測，並可開關 09 中央電子的隱藏功能。有圖形介面，也有指令列與 `--json` 輸出
(方便 AI / 程式操作)。

開發與實測的車：**Skoda Yeti 1.2 TSI 2014**，搭配便宜的 ELM327 v1.5 仿製接頭與 Vgate V-LINK (v2.1)。
同平台 (Octavia II、Golf 5/6、Superb II…) 可能部分可用，但**沒有測過**。歡迎車友測試回報。

> 手機網頁版 (Android Chrome + 藍牙接頭，唯讀，開發暫停)：https://ilshang2.github.io/yeti-diag/
> 主要功能都在下面的 Python 程式。

> ## ⚠️ 使用風險自負
>
> - 這個工具會跟車上的控制模組通訊。**寫入**設錯可能讓功能失效、產生故障碼，最壞會讓模組無法使用。
> - 讀取設計成不會改車，但便宜接頭、電瓶電壓不足仍可能出問題。建議引擎發動或接充電器再用。
> - **寫入功能是實驗性質**，只在作者的一台車上驗證過。不確定就只用讀取。
> - **行駛中只能看「即時儀表」(引擎 OBD)**。程式會在車速超過 3 km/h 時拒絕連 ABS 等舊模組與任何寫入：
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
| 09 長編碼寫入 (隱藏功能開關) | **實驗**：實車寫入成功過一次 (原值寫回)，寫前備份、寫後讀回 |
| UDS 模組寫入 / 清除故障碼 | **實驗**：模擬器通過，實車很少測 |
| 圖形介面 (Windows) | 新功能，模擬器通過，實車測試中 |

## 安裝 (Windows)

1. 安裝 [Python 3.13](https://www.python.org/downloads/) (安裝時勾選 *Add python.exe to PATH*)。
2. 下載這個 repo (綠色 Code → Download ZIP，或 `git clone`)。
3. 在資料夾裡開 PowerShell，安裝套件到 `.deps`：
   ```
   py -3.13 -m pip install -r requirements.txt --target .deps
   ```
4. 藍牙接頭插上車、轉鑰匙到 ON，在 Windows「藍牙與其他裝置」配對 (PIN 通常是 `1234` 或 `0000`)。
   配對後到「更多藍牙設定 → COM 連接埠」看「傳出」的 COM 號碼。

不接車也能先試：所有指令都可以加 `--sim` 用內建模擬器。

## 圖形介面

```
py -3.13 tools\gui.py
```

選接頭的 COM 埠 → 「連線 / 讀電壓」。分頁：

- **即時儀表**：車速、轉速、水溫、電壓、進氣 / 外氣溫度，只讀引擎，行駛中可用，可存檔。
- **健康檢查**：各缸失火、電瓶電壓、排放自檢、引擎 OBD。
- **故障碼**：全車掃描 (要停車)。
- **隱藏功能**：讀 09 目前的開關 → 勾選 → 確認新舊值後寫入 (實驗)。

想要單一 exe：`py -3.13 -m pip install pyinstaller`，再
`py -3.13 -m PyInstaller --onefile --noconsole --name YetiDiag --paths .deps --paths . --add-data "profiles;profiles" --collect-submodules yeti_diag --hidden-import serial --hidden-import serial.tools.list_ports tools\gui.py`。
exe 的報告與備份存在 `文件\YetiDiag`。

## 指令列快速上手

```
.\yeti.ps1 --sim autoscan             # 不接車，用模擬器試跑全車掃描
.\yeti.ps1 --elm COM5 voltage         # 電瓶電壓 (COM5 換成你的埠)
.\yeti.ps1 --elm COM5 autoscan        # 全車掃描，存到 reports\
.\yeti.ps1 --elm COM5 obd             # 引擎即時數據與故障碼
.\yeti.ps1 --elm COM5 misfire         # 各缸失火
.\yeti.ps1 --elm COM5 readiness       # 排放自我檢測
.\yeti.ps1 --elm COM5 features        # 09 隱藏功能目前狀態
.\yeti.ps1 --elm COM5 commands        # 所有指令
py -3.13 tools\drive_log.py --elm COM5   # 行駛記錄 (只讀引擎)，存到 reports\*_drive.jsonl
```

所有指令加 `--json` 會輸出固定格式的 JSON，說明見 [AI_GUIDE.md](AI_GUIDE.md)。
完整技術規格見 [規格書.md](規格書.md)，英文說明 [README_en.md](README_en.md)。

## 寫入的安全設計

每次寫入都會依序：拒絕黑名單模組 (01 引擎、03 ABS、15 安全氣囊、25 防盜、44 轉向輔助) → 比對零件號碼 →
讀出舊值存成備份 (`backups\`) → 電壓須 ≥ 12.2 V → 車子須靜止 → 顯示新舊值要你確認 →
**只送一次** (失敗不自動重試) → 寫後讀回比對。可以用 `restore` 從備份還原。

## 別的車

模組清單、CAN ID、零件號碼都在 `profiles\skoda_yeti_2014.json`。別的車可以先跑只讀的
`.\yeti.ps1 --elm COM5 discover` 自動產生設定檔，再設環境變數 `YETI_DIAG_PROFILE=檔名`。

## 幫忙測試

1. 先只跑讀取：`voltage`、`autoscan`、`misfire`、`readiness`，或圖形介面的前三個分頁。
2. 到 GitHub **Issues** 回報：車型 / 年份 / 引擎、接頭型號、哪個指令、結果是否跟 VCDS / Car Scanner 一樣。
3. 貼報告前請**刪掉自己的車身號碼 (VIN)**：`reports\` 裡的檔案會記錄從車上讀到的 VIN。

## 測試 (開發者)

```
$env:PYTHONPATH = ".deps;."; py -3.13 -m pytest tests -q -n auto
```

全部在內建模擬器上執行，不需要接車。
