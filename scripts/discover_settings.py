"""List likely settings files without opening game memory or editing files."""
import argparse
import json
import os
import re
import stat
from pathlib import Path

HINTS = {
    'thefinals': [('local', 'Discovery/Saved/SaveGames/EmbarkOptionSaveGame.sav'),
                  ('local', 'Discovery/Saved/Config/WindowsClient/GameUserSettings.ini')],
    'battlefield6': [('documents', 'Battlefield 6/settings')],
    'holdfastnationsatwar': [('low', 'Anvil Game Studio/Holdfast NaW/HoldfastOptions.ini')],
    'holdfast': [('low', 'Anvil Game Studio/Holdfast NaW/HoldfastOptions.ini')],
}


def normalized(text):
    return re.sub(r'[^a-z0-9]', '', text.lower())


def known_roots():
    home = Path.home()
    roots = {'local': Path(os.environ.get('LOCALAPPDATA', home/'AppData/Local')),
             'roaming': Path(os.environ.get('APPDATA', home/'AppData/Roaming')),
             'low': home/'AppData/LocalLow', 'documents': home/'Documents',
             'saved': home/'Saved Games', 'config': home/'.config'}
    if os.name == 'nt':
        # Resolve redirected Documents (e.g. OneDrive) through the Windows shell.
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            roots['documents'] = Path(buffer.value)
    return roots


def reparse(path):
    try:
        s = path.lstat()
        return stat.S_ISLNK(s.st_mode) or bool(getattr(s, 'st_file_attributes', 0) & 0x400)
    except OSError:
        return True


def discover(game, explicit_roots=None, aliases=None, max_files=5000):
    roots = known_roots()
    explicit_roots = [Path(p).expanduser().resolve() for p in (explicit_roots or [])]
    needles = {normalized(x) for x in [game] + list(aliases or []) if normalized(x)}
    if not needles:
        raise ValueError('A nonempty game name or alias is required')
    candidates = list(explicit_roots)
    errors = []
    if not explicit_roots:
        for root_name, relative in HINTS.get(normalized(game), []):
            candidates.append(roots[root_name]/relative)
        for root in roots.values():
            if not root.is_dir(): continue
            try:
                candidates.extend(p for p in root.iterdir() if p.is_dir() and
                                  any(n in normalized(p.name) for n in needles))
            except OSError as error:
                errors.append({'path':str(root), 'error':str(error)})
    results, seen = [], set()
    examined = 0
    truncated = False
    def eligible(path):
        return path.suffix.lower() in ('.ini','.cfg','.conf','.json','.xml','.sav') or path.name.lower().startswith('profsave')
    def add(path):
        key = str(path.resolve())
        if key in seen or not eligible(path): return
        seen.add(key)
        try:
            s=path.stat()
            results.append({'path':key,'bytes':s.st_size,'modified_unix_s':s.st_mtime,
                            'hint':'Candidate only; verify active profile, keys and encoding before use.'})
        except OSError as error:
            errors.append({'path':str(path),'error':str(error)})
    for root in candidates:
        if truncated: break
        if not root.exists(): continue
        if root.is_file(): add(root); continue
        if reparse(root):
            errors.append({'path':str(root),'error':'Reparse/symlink root skipped; supply the resolved local directory.'})
            continue
        def onerror(error): errors.append({'path':getattr(error,'filename',''), 'error':str(error)})
        for directory, dirs, files in os.walk(str(root), followlinks=False, onerror=onerror):
            depth=len(Path(directory).relative_to(root).parts)
            dirs[:] = [d for d in dirs if depth < 6 and not reparse(Path(directory)/d)
                       and d.lower() not in ('node_modules','.git','cache','shadercache')]
            # Bound directories as well as files to keep large install scans finite.
            examined += 1
            for name in files:
                examined += 1
                if examined > max_files:
                    truncated=True; break
                path=Path(directory)/name
                if not reparse(path): add(path)
            if examined > max_files:
                truncated=True; break
    return {'schema':'game-sensitivity/discovery-v1','game':game,'candidates':sorted(results,key=lambda x:x['path']),
            'examined_entries':examined,'truncated':truncated,'errors':errors,
            'note':'Read-only location discovery. Paths are hints, not proof of active settings. Unsupported games may need an install root, publisher alias, launcher profile, registry or in-game settings view.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game',required=True)
    parser.add_argument('--root',action='append',help='Search this bounded directory instead of default user-folder hints')
    parser.add_argument('--alias',action='append',default=[])
    parser.add_argument('--max-files',type=int,default=5000)
    args=parser.parse_args()
    if not 1 <= args.max_files <= 50000:parser.error('max-files must be between 1 and 50000')
    try: print(json.dumps(discover(args.game,args.root,args.alias,args.max_files),indent=2))
    except (OSError,ValueError) as error:parser.exit(2,str(error)+'\n')


if __name__=='__main__':main()
