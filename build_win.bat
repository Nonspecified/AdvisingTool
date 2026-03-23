@echo off
REM Build a standalone Windows .exe (no Python install needed on target machine).
REM Run once from this directory in a command prompt.
REM Output: dist\AdvisingBot.exe  (share this file)
REM
REM First run on a new machine:
REM   python -m venv .venv
REM   .venv\Scripts\activate
REM   pip install -r requirements.txt
REM   build_win.bat

setlocal enabledelayedexpansion

set DATA_FLAGS=
if exist curricula_registry.json set DATA_FLAGS=!DATA_FLAGS! --add-data "curricula_registry.json;."
if exist AHelectives.csv          set DATA_FLAGS=!DATA_FLAGS! --add-data "AHelectives.csv;."
if exist SSelectives.csv          set DATA_FLAGS=!DATA_FLAGS! --add-data "SSelectives.csv;."
if exist TE_Rules.csv             set DATA_FLAGS=!DATA_FLAGS! --add-data "TE_Rules.csv;."

for %%f in (curriculum_*.csv) do (
    set DATA_FLAGS=!DATA_FLAGS! --add-data "%%f;."
)
for %%f in (*_TE.csv) do (
    set DATA_FLAGS=!DATA_FLAGS! --add-data "%%f;."
)
for %%f in (minor_*.csv) do (
    set DATA_FLAGS=!DATA_FLAGS! --add-data "%%f;."
)

pyinstaller --onefile --windowed --name "AdvisingBot" ^
  --hidden-import cmath ^
  --hidden-import pandas._libs.testing ^
  --hidden-import pandas._libs.tslibs.base ^
  --hidden-import numpy._core._exceptions ^
  --exclude-module numpy.tests ^
  --exclude-module pandas.tests ^
  %DATA_FLAGS% AdvisingBot.py

echo.
echo Build complete: dist\AdvisingBot.exe
