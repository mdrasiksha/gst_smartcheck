@echo off
cd /d %~dp0
python\pythonw.exe -m streamlit run app.py
exit

