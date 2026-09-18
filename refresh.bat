@echo off
REM Rafraichissement hebdomadaire du tableau de bord Meta Ads.
REM A declencher par le Planificateur de taches Windows.
REM
REM Le chemin de l'interpreteur est explicite : le Planificateur n'herite pas
REM forcement du PATH de la session interactive.

cd /d "C:\Users\User\Desktop\meta_automation\meta-ads-backfill"
"C:\Users\User\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" refresh_all.py --skip-images >> refresh.log 2>&1
exit /b %errorlevel%
