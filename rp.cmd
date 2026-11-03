@echo off
rem PR-Agent 交互式控制台一键启动（在任意目录运行都会正确定位项目）
"%~dp0.venv\Scripts\python.exe" "%~dp0launch.py" %*
