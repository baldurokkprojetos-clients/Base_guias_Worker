@echo off
cd /d "%~dp0"

echo ==========================================
echo      BASE GUIAS MANAGER - BUILDER
echo ==========================================

echo.
echo [1/4] Cleaning previous build artifacts...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist *.spec del *.spec

echo.
echo [2/4] Installing and Verifying Dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo.
echo [3/4] Repairing commonly corrupted packages...
echo     - Reinstalling MarkupSafe...
python -m pip install --force-reinstall MarkupSafe
echo     - Reinstalling Pillow (PIL)...
python -m pip install --force-reinstall Pillow

echo.
echo [4/4] Compiling Executable...
echo     This process may take several minutes. Please wait.

python -m PyInstaller --clean --noconsole --onefile ^
    --name "BaseGuiasManager" ^
    --hidden-import=Worker ^
    --hidden-import=uvicorn ^
    --hidden-import=fastapi ^
    --hidden-import=sqlalchemy.sql.default_comparator ^
    --hidden-import=pystray ^
    --hidden-import=PIL ^
    --hidden-import=PIL._tkinter_finder ^
    --collect-all=pystray ^
    --exclude-module=PIL._avif ^
    --exclude-module=PIL._webp ^
    --icon=NONE ^
    gui.py

echo.
if exist "dist\BaseGuiasManager.exe" (
    echo ==========================================
    echo           BUILD SUCCESSFUL!
    echo ==========================================
    echo The executable is ready at:
    echo %~dp0dist\BaseGuiasManager.exe
    echo.
) else (
    echo ==========================================
    echo             BUILD FAILED
    echo ==========================================
    echo Please check the error messages above.
)
pause
