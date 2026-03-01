@echo off
title Invoice Automation - Machine ID Generator

echo ================================
echo   Invoice Automation Tool
echo   Machine ID Generator
echo ================================
echo.

for /f "skip=1 tokens=2 delims==" %%A in ('wmic csproduct get uuid /value') do set MACHINEID=%%A

echo Machine ID:
echo %MACHINEID%
echo.
echo ================================
echo COPY this Machine ID and send to support.
echo ================================
echo.

pause

