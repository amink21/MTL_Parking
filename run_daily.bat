@echo off
cd /d C:\Users\Amin\Documents\MTL_cops\montreal-vibe
chcp 65001 >nul
C:\Python310\python.exe -X utf8 scanner_v2.py >> logs\scanner.log 2>&1
