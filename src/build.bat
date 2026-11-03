@echo off
rem Builds RefitStudio.exe with the C# compiler of .NET Framework 4 (included in Windows 10/11).
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe
cd /d "%~dp0"
"%CSC%" /nologo /target:winexe /optimize+ /codepage:65001 /out:..\RefitStudio.exe ^
  /win32icon:icon.ico ^
  /r:System.Web.Extensions.dll /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll ^
  /r:System.Windows.Forms.dll /r:System.Drawing.dll Launcher.cs
