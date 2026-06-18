@echo off
chcp 65001 >nul

if "%~1" NEQ "__RUN__" (
  start "Arti CRM password reset" cmd /k ""%~f0" __RUN__"
  exit /b
)

setlocal
cd /d C:\crm_marketplaces

echo ========================================
echo Arti CRM admin password reset
echo Current folder: %CD%
echo ========================================
echo.

if not exist "app\db.py" (
  echo ERROR: Project files were not found in C:\crm_marketplaces
  echo Please make sure the project is in C:\crm_marketplaces
  echo.
  echo This window will stay open. Copy this text to ChatGPT.
  goto :end
)

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=py"
)

echo Using Python: %PY%
echo.

> reset_admin_temp.py echo from app.db import init_db
>> reset_admin_temp.py echo from app import repository as r
>> reset_admin_temp.py echo USERNAME = 'admin'
>> reset_admin_temp.py echo PASSWORD = 'Admin2026!'
>> reset_admin_temp.py echo init_db()
>> reset_admin_temp.py echo users = r.list_users()
>> reset_admin_temp.py echo print('Users before reset:', users)
>> reset_admin_temp.py echo admin = None
>> reset_admin_temp.py echo for u in users:
>> reset_admin_temp.py echo ^    if str(u.get('username','')).lower() == USERNAME:
>> reset_admin_temp.py echo ^        admin = u
>> reset_admin_temp.py echo ^        break
>> reset_admin_temp.py echo if admin:
>> reset_admin_temp.py echo ^    r.update_user_password(int(admin['id']), PASSWORD)
>> reset_admin_temp.py echo ^    r.update_user(int(admin['id']), role='admin', is_active=True)
>> reset_admin_temp.py echo ^    print('OK: existing admin password reset')
>> reset_admin_temp.py echo else:
>> reset_admin_temp.py echo ^    r.create_user(USERNAME, PASSWORD, 'Admin', 'admin')
>> reset_admin_temp.py echo ^    print('OK: admin user created')
>> reset_admin_temp.py echo print('Users after reset:', r.list_users())
>> reset_admin_temp.py echo print('')
>> reset_admin_temp.py echo print('LOGIN: admin')
>> reset_admin_temp.py echo print('PASSWORD: Admin2026!')

%PY% reset_admin_temp.py
set ERR=%ERRORLEVEL%
del reset_admin_temp.py >nul 2>nul

echo.
if "%ERR%"=="0" (
  echo SUCCESS.
  echo Now restart the CRM server and log in with:
  echo LOGIN: admin
  echo PASSWORD: Admin2026!
) else (
  echo ERROR: reset failed with code %ERR%.
  echo Copy all text from this window and send it to ChatGPT.
)

:end
echo.
echo Press any key to close this window.
pause >nul
