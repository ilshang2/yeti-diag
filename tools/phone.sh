#!/bin/sh
# Phone helper for recording Car Scanner over adb: tap X Y | shot NAME | key CODE | swipe ...
ADB="${ADB:-adb}"
OUT="$(dirname "$0")/../reports/phone"
mkdir -p "$OUT"
case "$1" in
  tap) "$ADB" shell input tap "$2" "$3" ;;
  key) "$ADB" shell input keyevent "$2" ;;
  swipe) "$ADB" shell input swipe "$2" "$3" "$4" "$5" "${6:-400}" ;;
  shot) "$ADB" exec-out screencap -p > "$OUT/$2.png" && py -3.13 -c "
from PIL import Image; im=Image.open(r'$OUT/$2.png'); im.thumbnail((540,1200)); im.save(r'$OUT/$2s.png')" ;;
esac
