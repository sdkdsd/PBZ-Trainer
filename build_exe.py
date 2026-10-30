"""Build only the verified panel and its runtime as a Windows one-file EXE."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import struct
import sys

import PyInstaller.__main__

SOURCE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=SOURCE / 'dist')
    args = parser.parse_args()
    if sys.platform != 'win32' or struct.calcsize('P') != 8 or sys.version_info[:2] != (3, 13):
        raise RuntimeError('Build with Windows x64 Python 3.13 in an isolated environment')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = '影之刃零修改器'
    exe = output / (name + '.exe')
    if any(path != exe for path in output.iterdir()):
        raise RuntimeError('Use a dedicated output directory containing only the intended EXE')
    work = SOURCE / '.build' / 'onefile'
    work.mkdir(parents=True, exist_ok=True)
    version = work / 'version.txt'
    version.write_text("""VSVersionInfo(
  ffi=FixedFileInfo(filevers=(0,13,1,0), prodvers=(0,13,1,0), mask=0x3f, flags=0x0,
                   OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
  kids=[StringFileInfo([StringTable('080404b0', [
    StringStruct('FileDescription', '影之刃零修改器'),
    StringStruct('FileVersion', '0.13.1'),
    StringStruct('InternalName', 'PBZTrainer'),
    StringStruct('OriginalFilename', '影之刃零修改器.exe'),
    StringStruct('ProductName', '影之刃零修改器'),
    StringStruct('ProductVersion', '0.13.1')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])])
""", encoding='utf8')
    inputs = ['app.py', 'core.py', 'errors.py', 'ui_theme.py', 'session_effects.py', 'teleport_unlock.py', 'runtime_paths.py', 'profile.json',
              'assets/app.ico', 'assets/THIRD_PARTY_NOTICES.txt']
    for relative in inputs:
        if not (SOURCE / relative).is_file():
            raise RuntimeError('Missing build input: ' + relative)
    PyInstaller.__main__.run([
        '--noconfirm', '--onefile', '--windowed', '--noupx', '--name', name,
        '--distpath', str(output), '--workpath', str(work / 'work'), '--specpath', str(work),
        '--paths', str(SOURCE), '--icon', str(SOURCE / 'assets/app.ico'),
        '--version-file', str(version), '--add-data', str(SOURCE / 'profile.json') + ':.',
        '--add-data', str(SOURCE / 'assets') + ':assets', str(SOURCE / 'app.py')])
    files = list(output.iterdir())
    if files != [exe] or not exe.is_file():
        raise RuntimeError('Output must contain exactly one EXE')
    report = {'exe': str(exe), 'bytes': exe.stat().st_size, 'sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
              'python': sys.version, 'pyinstaller': importlib.metadata.version('pyinstaller'),
              'hooks': importlib.metadata.version('pyinstaller-hooks-contrib'),
              'onefile': True, 'console': False, 'upx': False, 'output_files': [exe.name],
              'input_sha256': {relative: hashlib.sha256((SOURCE / relative).read_bytes()).hexdigest() for relative in inputs}}
    (work / 'build-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
