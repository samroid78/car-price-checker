@echo off
title UK Car Price Checker

echo.
echo  ============================================
echo   UK Car Price Checker
echo  ============================================
echo.
echo  Installing / checking dependencies...
pip install -r requirements.txt --quiet
echo.
echo  Installing Playwright browsers (first run only)...
python -m playwright install chromium --quiet 2>nul
echo.
echo  Starting server...
echo  Open your browser and go to: http://localhost:5000
echo.
echo  Press Ctrl+C to stop.
echo  ============================================
echo.
python app.py
pause
