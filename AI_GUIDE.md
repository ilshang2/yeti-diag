# yeti_diag 給 AI 的使用說明

這份文件寫給要操作這個工具的 AI 模型 (以及寫腳本的人)。
目標：用一般藍牙 OBD 接頭 (ELM327) 做到 VCDS 的事，而且每個結果都是可以直接讀的 JSON，
不需要截圖、不需要滑鼠。

## 呼叫方式

```
.\yeti.ps1 --elm COM9 --json <指令> [參數]
```

- `--json`：stdout **只有一份 JSON**；過程訊息在 stderr，可以忽略或給人看。
- 不接車測試：把 `--elm COM9` 換成 `--sim`。
- 先執行 `--json commands` 取得所有指令、參數、哪些會寫入車輛。

## 用 MCP 直接呼叫 (Claude 等支援 MCP 的 AI)

`yeti_diag.mcp_server` 把每個指令變成一個工具 (不需安裝套件)。這個資料夾已經有 `.mcp.json`
(伺服器名稱 `yeti-diag`，接頭 `auto`)：在 yeti-diag 資料夾開 Claude Code，第一次會問要不要啟用。
`--elm auto` 會逐一試 COM 埠找回應 ATZ 的 ELM327 (上次成功的埠先試，記在 `reports\elm_port.txt`)；
接頭沒醒 (鑰匙沒轉 ON、接頭睡眠) 時約 10 秒後回 connection 錯誤。也可以寫死，例如 `"--elm", "COM10"`。

不接車先試：把 `"--elm", "auto"` 換成 `"--sim"`。工具的回傳內容就是下面的 JSON 格式。
`sniff` 經 MCP 呼叫時 `seconds` 必填 (最多 600 秒)，因為沒有人能替 AI 按 Ctrl+C。
寫入工具沒有 `yes` 參數，只能產生計畫；`apply` 的說明裡寫明要先取得人同意。
Claude Code 預設每次呼叫 MCP 工具都會問人；**不要把 `apply` 設成自動允許**。

## 回應格式 (每個指令都一樣)

```json
{
  "ok": true,
  "command": "autoscan",
  "tool": "yeti_diag",
  "version": "0.5.0",
  "time": "2026-11-03T18:00:00",
  "vin": "TMBJF65L0E6000000",
  "sim": false,
  "data": { "...": "指令自己的結果" },
  "error": null
}
```

失敗時 `ok` 是 false，`error` = `{"kind": "...", "message": "..."}`，`message` 是給人看的中文。

## 結束碼

| 碼 | kind | 意思 | AI 該怎麼做 |
|---|---|---|---|
| 0 | ok | 成功 | — |
| 1 | check_failed | 執行完成但結果不全好 (例如 verify 有失敗項、某模組讀不到) | 讀 `data` 看哪一項 |
| 2 | usage | 參數錯、模組不認得、還沒有 TP2.0 位址表 | 照 `message` 修正指令 |
| 3 | connection | 接頭或模組沒回應 | 先 `voltage`；確認點火開關 ON、接頭有配對；可再試一次讀取 |
| 4 | refused | 安全檢查拒絕寫入，或寫入計畫不符/過期/已執行 | **不要繞過**，回報給人 |
| 5 | verify_failed | 寫入已送出，但讀回的值不對 | **立刻停止**，回報給人，附上備份檔路徑 |
| 70 | internal | 工具本身的錯誤 | 回報，附 `error.traceback` |

## 常用讀取指令 (不會改車)

| 指令 | VCDS 對應 | 說明 |
|---|---|---|
| `voltage` | — | 電瓶電壓。讀寫前先看，低於 12.2 V 不能寫入 |
| `autoscan` | Auto-Scan | 全部模組：零件號碼、編碼、故障碼 (含中文說明、是否間歇性) |
| `kwp-dtc 62` | 舊模組 Fault Codes | 單一舊模組 (TP2.0) 的故障碼 |
| `dtc 17` | 新模組 Fault Codes | 單一 UDS 模組的故障碼 |
| `measure 19 1 2 3` | Measuring Blocks | 舊模組測量群組，數值 + 單位；設定檔有名稱的會附 `name` |
| `values` | — | 已在實車確認的數值 (17 總里程、01 扭力 / 機油油位 / 增壓 / 機油溫度 / 油量 / 檔位) |
| `candidates` | — | 設定檔裡還沒確認的 DID / 群組原始值，拿去對照其他軟體畫面 |
| `sniff --seconds 120 --module 17` | — | 只聽不發，錄 VCDS (Y 接線) 的封包到 reports。MCP 呼叫一定要給 `--seconds` |
| `sniff-analyze <檔> --vcds <csv>` | — | 還原請求/回覆、列出寫入等動作；加 VCDS CSV 自動算出數值的 DID 與換算 |
| `profile` | — | 目前的車型設定檔、可用的設定檔、設定檔格式問題 |
| `features` | 長編碼小幫手 | 09 中央電子隱藏功能目前開或關 |
| `obd` | — | 引擎 OBD 即時數據 |
| `readiness` | — | 排放自我檢測完成狀態 (VCDS Readiness)：`mil`、`dtc_count`、`monitors[]` (`name`、`complete`)、`all_complete` |
| `battery` | `--crank 秒數` (選填，最多 120) | 電瓶：`snapshot` (接頭 / 引擎 / 閘道器電壓、rpm)；熄火時 `rest.soc` 估計電量，運轉時 `charging.ok`；`--crank` 要先熄火、點火 ON，開始後請使用者轉鑰匙，回 `crank.min` / `grade` (good / weak / replace) |
| `verify` | — | 跟 2026-11-03 的 VCDS 結果比對 18 項 (工具自我檢查) |

## 寫入 (會改車)：一定是兩步

1. **產生計畫**：在 `--json` 模式下執行寫入指令 (例如 `feature leaving-home on`)。
   工具會做完全部安全檢查、讀出舊值、存備份，然後**停下來不寫入**，回傳：
   ```json
   {"status": "planned", "plan": {"id": "3f9a1c2b", "summary": "...", "old": "...", "new": "...",
    "changes": [{"byte": 16, "old": "4F", "new": "4B", "bits": "00000100 關"}], "expires": "..."}}
   ```
   `changes` (長度相同的寫入才有)：哪個位元組 (從 0 起算) 從什麼變成什麼、哪些位元打開 / 關掉。
2. **把舊值、新值、說明給人看，取得人明確同意。** 用 `summary` + `changes` 說明，不要只貼兩串十六進位。
3. **執行**：`apply 3f9a1c2b`。工具會重新讀取，只有車上的值還等於計畫裡的舊值才寫入，
   而且**只送一次**。寫入後會讀回比對。

規則 (給 AI)：
- 沒有人同意，不可以執行 `apply`。不可以用 `--yes` 跳過。
- `apply` 失敗不可以自動重試；同一個計畫只能執行一次，過期 (15 分鐘) 要重新產生。
- 計畫會記住目標 (`sim`、`elm:COM9`)：模擬器產生的計畫不能拿到真車執行。
- 01 引擎、03 ABS、15 安全氣囊、25 防盜、44 轉向輔助一律拒絕寫入。
- 結束碼 5：停止所有寫入，回報備份檔路徑 (`backups/`)。

## 故障碼欄位

舊模組 (5 碼，例如 `02115`)：

| 欄位 | 意思 |
|---|---|
| `text` / `text_en` | 代碼說明；`null` 代表說明表沒有 |
| `symptom` / `symptom_text` | 故障類型 (例如 `008` 訊號不合理) |
| `state` | `intermittent` 間歇性 (讀取當下不存在) / `present now` 目前存在 |
| `present_now` / `intermittent` | 同上，布林值 |
| `status` | 原始狀態位元組 (十六進位) |

新模組 (UDS，例如 `P0300-00`)：只列真的故障 (失效、待確認、已確認)。
「測試還沒跑完」(狀態 0x10) 那種不算故障，跟 VCDS 一樣不列出。

## 數值可信度

- 故障碼說明只寫了本車實際對照過的代碼；`"known": false` 代表說明表還沒有，**不要自己猜意思**。
- 測量群組每個值都有 `"verified"`。`false` 代表換算公式來自公開的社群資料，還沒有在實車上
  跟其他讀數比對過，引用時要說明「未驗證」。
- `raw` 欄位一定保留原始位元組，可以自己重算。
- `values`、測量群組的 `name` 都附 `checked`，說明是怎麼在實車確認的；設定檔只收確認過的。

## 目前限制

- 車型資料在 `profiles/*.json`，目前只有本車 (Skoda Yeti 1.2 TSI 2014)。換車型：環境變數
  `YETI_DIAG_PROFILE=<名稱或路徑>`；新設定檔先跑 `profile` 看格式問題，再接車跑 `probe`、`tp20-probe`、`verify`。
- `clear-dtc` 新舊模組都能用 (舊模組 KWP `14 FF 00`)，2026-11-03 已在 72 右後門實車清除成功。
- 09 中央電子長編碼寫入：2026-11-06 實車寫入成功 (原值寫回驗證流程)；`feature` 仍屬實驗，每次都要人同意。
- **行駛中 (OBD 車速 > 3 km/h) 工具會拒絕連所有舊模組 (TP2.0) 與任何寫入** (結束碼 4 `refused`)：
  行駛中讀 03 ABS 會讓儀表叮聲、ABS / 車身穩定燈閃。只有引擎 OBD (`obd`、`misfire`、`readiness`) 行駛中可讀。**不要用 `--allow-moving` 繞過。**
- 接頭：傳統藍牙 (有 COM 埠) 與 USB 實車測過；BLE (`--elm BLE:名稱`) 有支援但還沒用實體接頭測過。
