# Refit Studio

Auto refit clothing for any female V body mod in Cyberpunk 2077.
Nexus Mods page: https://www.nexusmods.com/cyberpunk2077/mods/37166

## What is in this repository

- `src/` - the launcher (`RefitStudio.exe`), a single C# WinForms file.
- `core/` - Python scripts that run inside Blender (fitting, workshop panel, body profiles) and an empty `studio.blend` scene.

The release archive on Nexus contains exactly these files plus the built `RefitStudio.exe` and readme files.

## Build the launcher

Requirements: Windows 10 or 11. No extra tools needed - it uses the C# compiler that ships with Windows (.NET Framework 4, `csc.exe`).

1. Open the `src` folder.
2. Run `build.bat`.
3. `RefitStudio.exe` is created in the repository root.

`core/` is plain Python source, nothing to build.

## Run

Put `RefitStudio.exe` next to the `core` folder and start it. You also need Blender 4.2+ and WolvenKit CLI (the launcher can download WolvenKit from its official GitHub releases).
