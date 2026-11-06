# 怎麼參與 / How to contribute

**中文** · [English](#english)

這個專案要能支援更多車，靠的是車友一人回報一台車 (跟 [ddt4all](https://github.com/cedricp/ddt4all) 一樣的社群模式)。
你不用會寫程式。

## 1. 回報你的車 (最有幫助)

成功或失敗都請回報，**失敗的回報最有價值**。

1. 先只用讀取：圖形介面的前三個分頁，或 `voltage`、`autoscan`、`misfire`、`readiness`。
2. 開一個 [「回報我的車」Issue](https://github.com/ilshang2/yeti-diag/issues/new?template=car_report.yml)，照表單填車型、接頭、哪些正常。
3. **貼任何東西前刪掉車身號碼 (VIN，17 碼)**。`reports\` 裡的檔案會記錄從車上讀到的 VIN。

## 2. 幫你的車建立設定檔

內建設定檔是 Skoda Yeti 1.2 TSI 2014。其他車：

```
.\yeti.ps1 --elm COM5 discover --name golf6_2010 --title "VW Golf 6 1.4 TSI 2010"
```

- 只讀，不會改車。約 3-5 分鐘，掃描所有新舊模組，產生 `profiles\golf6_2010.json`。
- **VIN 後 6 碼 (車輛序號) 會自動遮蔽**，前 11 碼 (車廠、車型、年份、工廠) 保留，檔案可以直接分享。
- 用它：設環境變數 `YETI_DIAG_PROFILE=golf6_2010`，再跑 `profile`、`tp20-probe`、`autoscan`。
- 把 json 附在「回報我的車」Issue 裡，或發 Pull Request 放進 `profiles\`。

設定檔裡的數值名稱 (`uds_values`、`group_labels`) 只收**在實車對照過**的，每項都寫 `checked` 說明怎麼確認
(例如「行駛中跟 OBD 車速比對 12 次」)。不確定的放 `candidates`，不要猜。

## 3. 測量群組名稱：用你自己的標籤檔

測量群組 (`measure`) 預設只有實車確認過的名稱。如果你**自己有** VCDS 格式的**純文字**標籤檔 (`.lbl`)，可以讓程式讀：

```
.\yeti.ps1 --elm COM5 --labels C:\我的標籤 measure 01 1 2 3
```

或放在 `<資料夾>\labels` (exe 是 `文件\YetiDiag\labels`) 就不用加參數。

- 支援 `REDIRECT` 轉址與 `?` 萬用字元，依模組零件號碼找檔。
- 從標籤檔來的名稱會標 `"name_source": "label file"`，跟實車確認過的分開。
- **加密的 `.clb` 不支援** (只有 VCDS 能讀)，目前 VCDS 安裝內的標籤大多是這種。
- **標籤檔不要上傳到這個 repo**：大部分是 Ross-Tech 或其他作者的著作。

## 4. 程式

- Python 3.13，`py -3.13 -m pip install -r requirements.txt --target .deps`。
- 測試全部在內建模擬器上跑：`$env:PYTHONPATH = ".deps;."; py -3.13 -m pytest tests -q -n auto`。
- 新功能請附測試；改到通訊的部分請說明在哪台車、哪顆接頭測過。
- **寫入相關的改動**：一定要維持「先備份 → 給人看新舊值 → 只送一次 → 寫後讀回」，以及行駛中拒絕。
- 不接受：改里程、防盜破解、破解安全存取 (seed/key)、韌體刷寫。
- 授權：MIT。請不要貼入 GPL 或其他不相容授權的程式碼 (例如 vag-blocks 是 GPLv3)。

---

## English

The tool supports more cars only if owners report theirs, one car each (the [ddt4all](https://github.com/cedricp/ddt4all) model). No coding needed.

1. **Report your car** — success or failure, failures help most. Use reads only, then open a
   [car report](https://github.com/ilshang2/yeti-diag/issues/new?template=car_report.yml). **Remove your VIN** from anything you paste.
2. **Make a profile for your car** — `.\yeti.ps1 --elm COM5 discover --name golf6_2010 --title "VW Golf 6 1.4 TSI 2010"`.
   Read-only, 3–5 minutes, writes `profiles\golf6_2010.json` with the VIN serial (last 6) masked so it can be shared.
   Use it with `YETI_DIAG_PROFILE=golf6_2010`. Attach it to the car report or open a pull request.
   Value names only go in after being checked on a car, with a `checked` note saying how.
3. **Measuring-group names from your own label files** — `--labels <folder>` (or `<data>\labels`) reads plain-text
   VCDS-style `.lbl` files you own: `REDIRECT` and `?` wildcards supported, names marked `"name_source": "label file"`.
   Encrypted `.clb` files are not supported. **Do not upload label files here**: most are Ross-Tech's or other authors' work.
4. **Code** — Python 3.13, tests run on the built-in simulator. Keep the write safety flow (backup → show old/new →
   send once → read back) and the moving-car refusal. No odometer, immobilizer, seed/key or flashing work.
   MIT licence: no GPL code (vag-blocks is GPLv3).
