@echo off
chcp 936 >nul
cd /d "%~dp0"
title SoundCapture - first commit

echo ============================================================
echo   这一步只在你自己的电脑上提交代码，不会上传到网络。
echo   上传（push）由你自己执行下一步的那两条命令完成。
echo ============================================================
echo.

set "GHNAME="
set "GHMAIL="
set "GHREPO="
set /p GHNAME=1/3  你的 GitHub 用户名:
set /p GHMAIL=2/3  你的 GitHub 邮箱:
set /p GHREPO=3/3  仓库名（英文，例如 soundcapture）:

if "%GHNAME%"=="" goto :need
if "%GHMAIL%"=="" goto :need
if "%GHREPO%"=="" goto :need

echo.
echo -- 写入仓库级身份（不改动全局 git 配置）
git config user.name "%GHNAME%"
git config user.email "%GHMAIL%"

echo -- 把 LICENSE 里的版权名字换成你的
powershell -NoProfile -Command "$p='LICENSE'; if (Test-Path $p) { $t=[IO.File]::ReadAllText($p) -replace 'YOUR_NAME_HERE','%GHNAME%'; [IO.File]::WriteAllText($p,$t,(New-Object Text.UTF8Encoding($false))) }" >nul 2>&1

echo -- 暂存文件
git add .
echo.
echo 即将提交的文件：
git diff --cached --name-only
echo.
echo 确认上面没有 dist\ build\ 录音输出\ 之类的东西后，按任意键继续；
echo 要中止就直接关掉这个窗口。
pause

:: 提交信息刻意用英文：Windows 下中文 commit message 在 GitHub 上容易显示成乱码
git commit -m "feat: SoundCapture v1.0.0 - internal audio / mic / dual-track recording + floating bar"
if errorlevel 1 goto :fail

git branch -M main
git remote remove origin >nul 2>&1
git remote add origin https://github.com/%GHNAME%/%GHREPO%.git
git tag -f -a v1.0.0 -m "v1.0.0" >nul 2>&1

echo.
echo ============================================================
echo   本地已提交完成。现在按顺序执行下面两条命令上传：
echo.
echo     git push -u origin main
echo     git push -f origin v1.0.0
echo.
echo   执行时会弹窗让你登录 GitHub，选「用浏览器登录」最简单。
echo   前提：你已经在这个地址建好了空仓库
echo     https://github.com/%GHNAME%/%GHREPO%
echo   （建仓库时不要勾 README / .gitignore / license）
echo ============================================================
echo.
pause
goto :eof

:need
echo.
echo 用户名 / 邮箱 / 仓库名都不能为空，重新运行本脚本。
pause
goto :eof

:fail
echo.
echo 提交失败（通常是没有文件变化，或身份没配上）。按提示检查后重跑。
pause
