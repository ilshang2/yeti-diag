"""隱藏功能清單：09 中央電子 (BCM PQ35, 1K0 937 086 AA) 長編碼的單一 bit。

每一項的 byte / bit 位置，來自 2026-11-03 在本車上用 VCDS 長編碼輔助工具 (LCH)
看到的說明。只適用於零件號碼 1K0937086AA 的模組；其他版本的位置可能不同，
寫入前 writer.py 會檢查零件號碼。

verified:
  "實車寫入" - 已在本車寫入並確認有作用
  "標籤確認" - 位置來自 VCDS 說明，尚未用本工具寫入過
"""
from dataclasses import dataclass

from . import writer
from .vehicle import Module

CODING_DID = 0x0600
BCM = Module("09", "中央電子", "1K0 937 086 AA", 0, 0)  # CAN IDs unused: reached over TP2.0


@dataclass(frozen=True)
class Feature:
    key: str
    name: str
    byte: int
    bit: int
    note: str
    verified: str = "標籤確認"


FEATURES = [
    Feature("brake-flash", "急煞閃警示燈", 16, 2, "急煞時方向燈 (雙黃燈) 自動閃爍", "實車寫入"),
    Feature("fog-cornering", "霧燈轉向輔助照明", 14, 7, "低速轉彎時對應側前霧燈會亮 (需有前霧燈)", "實車寫入"),
    Feature("comfort-blink", "方向燈輕撥", 16, 0, "輕撥方向燈閃固定次數 (次數在 Adaptation 設定)"),
    Feature("rear-wiper-drip", "後雨刷點滴刷", 21, 4, "後雨刷噴水後幾秒多刷一次", "實車寫入"),
    Feature("rear-wiper-reverse", "倒車時後雨刷作動", 21, 6, "前雨刷開著時打 R 檔，後雨刷自動刷"),
    Feature("leaving-home", "Leaving Home 照明", 17, 6, "遙控解鎖時頭燈亮一段時間"),
    Feature("coming-home", "Coming Home 照明", 17, 5, "熄火下車後頭燈亮一段時間"),
    Feature("lock-blink", "上鎖時方向燈閃", 5, 0, "遙控上鎖/解鎖時方向燈閃一下確認"),
    Feature("lock-horn", "上鎖時喇叭響", 5, 1, "遙控上鎖時喇叭響一聲"),
    Feature("window-blink", "遙控開窗時方向燈閃", 5, 5, "長按遙控器開關車窗時方向燈閃爍提示"),
    Feature("mirror-dip", "倒車副駕後照鏡下傾", 4, 3, "打 R 檔時副駕後照鏡自動下傾 (需記憶後照鏡)"),
    Feature("auto-lock", "車速自動上鎖", 0, 2, "車速約 15 km/h 時自動上鎖"),
    Feature("auto-unlock", "熄火自動解鎖", 0, 1, "拔鑰匙時自動解鎖"),
    Feature("single-unlock", "單鍵解鎖", 0, 3, "按一次只開駕駛座，按兩次全開"),
    # 2026-11-05 LCH 全 30 bytes 畫面 (1K0 937 086 AA)
    Feature("drl", "日行燈", 15, 6, "日行燈 (獨立燈組) 啟用；2026-11-05 已關閉 (6C -> 2C)", "實車寫入"),
    Feature("drl-parking-brake", "拉手煞車時日行燈關", 11, 0, "手煞車拉起時日行燈不亮 (停車紅火省電)"),
    Feature("drl-turn-signal", "打方向燈時日行燈熄", 15, 3, "打方向燈時同側日行燈暫時熄滅，方向燈比較明顯"),
    Feature("drl-position", "日行燈兼位置燈", 24, 7, "開小燈時日行燈 (調暗) 當位置燈"),
    Feature("cornering-reverse", "倒車開轉向輔助燈", 26, 6, "打 R 檔時轉向輔助照明 (霧燈) 亮"),
    Feature("sunroof-remote-open", "遙控舒適開天窗", 3, 0, "長按遙控解鎖鍵時天窗打開"),
    Feature("sunroof-remote-close", "遙控舒適關天窗", 3, 1, "長按遙控上鎖鍵時天窗關閉"),
    Feature("comfort-remote", "遙控舒適開關窗", 3, 5, "長按遙控器開關車窗"),
    Feature("rain-closing", "下雨自動關窗", 4, 6, "下雨時自動關窗 / 天窗 (需雨量感測器，本車沒有)"),
    Feature("unlock-horn", "解鎖時喇叭響", 5, 4, "遙控解鎖時喇叭響"),
    Feature("windows-after-off", "熄火後可開關窗", 4, 7, "熄火後 (開門前) 還能操作電動窗 / 天窗"),
]
BY_KEY = {f.key: f for f in FEATURES}


def is_on(coding, f):
    return bool(coding[f.byte] & (1 << f.bit))


def with_feature(coding, f, on):
    new = bytearray(coding)
    if on:
        new[f.byte] |= 1 << f.bit
    else:
        new[f.byte] &= ~(1 << f.bit) & 0xFF
    return bytes(new)


def read_coding(client):
    return client.read_did(CODING_DID)


def set_feature(client, f, on, *, voltage, confirm, backup_dir=None):
    """Flip one bit of the 09 long coding through writer.write_did (backup, checks, verify)."""
    new = with_feature(read_coding(client), f, on)
    return writer.write_did(client, BCM, CODING_DID, new, voltage=voltage, confirm=confirm,
                            backup_dir=backup_dir)


def rewrite_coding(client, *, voltage, confirm, backup_dir=None):
    """Write the 09 long coding back unchanged: proves the write sequence, changes nothing."""
    old = read_coding(client)
    return writer.write_did(client, BCM, CODING_DID, old, voltage=voltage, confirm=confirm,
                            backup_dir=backup_dir, same_ok=True)
