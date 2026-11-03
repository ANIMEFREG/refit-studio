# Общее для лаунчера и мастерской: настройки, папки данных, WolvenKit, поиск модов.
import os, re, json, hashlib, subprocess, datetime, struct

APP = 'Refit Studio'
DATA = os.environ.get('REFIT_DATA') or os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'RefitStudio')
CONFIG = os.path.join(DATA, 'config.json')
CACHE = os.path.join(DATA, 'cache')          # файлы обычного тела из игры
PROFILES = os.path.join(DATA, 'bodies')      # профили тел
WORK = os.path.join(DATA, 'w')               # рабочие папки модов (короткий путь: длинные ломали экспорт)
SAVES = os.path.join(DATA, 'saves')
EXPORTS = os.path.join(DATA, 'exports')      # собранные zip (отсюда их берёт Vortex и копирует себе)
LOG = os.path.join(DATA, 'log.txt')


def _lang():
    try:
        with open(CONFIG, encoding='utf-8') as f:
            return 'ru' if json.load(f).get('lang') == 'ru' else 'en'
    except Exception:
        return 'en'


LANG = os.environ.get('REFIT_LANG') or _lang()  # язык интерфейса: 'en' (по умолчанию) или 'ru'


def tr(ru, en):
    """Текст на выбранном языке интерфейса."""
    return ru if LANG == 'ru' else en


def lp(path):
    """Путь, который Windows понимает и длиннее 260 символов (пути внутри архивов игры очень длинные)."""
    path = os.path.abspath(path)
    if os.name != 'nt' or path.startswith('\\\\?\\'):
        return path
    if path.startswith('\\\\'):  # сетевой путь \\server\share
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path


def rmtree(path):
    """Удаляет рабочую папку целиком (в том числе с длинными путями)."""
    import shutil
    shutil.rmtree(lp(path), ignore_errors=True)


def work_dir(tag):
    """Короткая временная папка для распаковки (в TEMP), чтобы пути из архивов не упирались в лимит."""
    import tempfile
    return tempfile.mkdtemp(prefix='rs_' + tag + '_')
OUR_PREFIXES = ('#refit_', '#slim_thin_refit_')  # архивы, которые сделали мы сами

PFA = 'base\\characters\\common\\player_base_bodies\\player_female_average\\'
BODY_FILES = {
    'torso': PFA + 't0_000_pwa_base__full.mesh',
    'arm_l': PFA + 'arms_hq\\a0_000_pwa_base_hq__l.mesh',
    'arm_r': PFA + 'arms_hq\\a0_000_pwa_base_hq__r.mesh',
    'feet': PFA + 'l0_000_pwa_base__cs_flat.mesh',
}
TATTOO_FILES = {f'tattoo_0{i}': PFA + f'tattoos\\tx_000_pwa_base__full_tattoo_0{i}.mesh' for i in range(1, 6)}
PART_NAMES = {'torso': tr('Тело и ноги', 'Torso and legs'), 'arm_l': tr('Левая рука', 'Left arm'),
              'arm_r': tr('Правая рука', 'Right arm'), 'feet': tr('Ступни', 'Feet')}


def part_name(k):
    return PART_NAMES.get(k) or k.replace('tattoo_', tr('татуировка ', 'tattoo '))


def log(msg):
    os.makedirs(DATA, exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(f'{datetime.datetime.now():%d.%m %H:%M:%S} {msg}\n')


def load_config():
    try:
        return json.load(open(CONFIG, encoding='utf-8-sig'))
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(DATA, exist_ok=True)
    tmp = CONFIG + '.tmp'
    json.dump(cfg, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)


def short_id(text, n=8):
    return hashlib.sha1(text.lower().encode('utf-8')).hexdigest()[:n]


def safe(name, n=60):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('_')[:n] or 'x'


def clean_mod_name(n):
    """'SLIM THIN BODY (KS-UV) 33451 4 2026-09-18T19-41Z AgoGgiowW' -> 'SLIM THIN BODY (KS-UV)',
    'Zenitex Combat T-Shirt-26406-1-3-1778794928' -> 'Zenitex Combat T-Shirt'."""
    s = re.sub(r'\s+\d{3,6}(\s+[\d.]+)*\s+20\d\d-\d\d-\d\dT\S+(\s+\S+)?$', '', n)
    s = re.sub(r'[-_ ]\d{3,6}([-_. ]\d+)+$', '', s)
    s = re.sub(r'_\d{3,6}(_[\d.]+)*_20\d\d-\d\d-\d\dT\S+$', '', s)
    return s.strip(' -_') or n


def fs_name(name):
    """Имя файла Windows без запрещённых символов."""
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def output_dir(cfg=None):
    cfg = cfg or load_config()
    d = os.environ.get('REFIT_OUTPUT') or cfg.get('output') or os.path.join(os.path.expanduser('~'), 'Desktop')
    os.makedirs(d, exist_ok=True)
    return d


# ---------- Vortex ----------

def find_vortex(cfg=None):
    cfg = cfg or load_config()
    cands = [cfg.get('vortex', '')]
    for pf in (os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('ProgramFiles(x86)', '')):
        if pf:
            cands += [os.path.join(pf, 'Black Tree Gaming Ltd', 'Vortex', 'Vortex.exe'),
                      os.path.join(pf, 'Vortex', 'Vortex.exe')]
    cands.append(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Vortex', 'Vortex.exe'))
    return next((c for c in cands if c and os.path.isfile(c)), '')


def send_to_vortex(vortex, zip_path):
    """Отдаёт архив Vortex на установку (ключ --install-archive). Если Vortex уже открыт,
    команду получит открытое окно. REFIT_NO_VORTEX=1 - только записать в лог (для тестов)."""
    zip_path = os.path.abspath(zip_path)
    if os.environ.get('REFIT_NO_VORTEX'):
        log(f'[test] Vortex: {vortex} --install-archive {zip_path}')
        return
    subprocess.Popen([vortex, '--install-archive', zip_path], close_fds=True,
                     creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))


# ---------- WolvenKit ----------

def wk(*args):
    exe = load_config().get('wolvenkit', '')
    if not os.path.isfile(exe):
        raise RuntimeError(tr('Не найден WolvenKit CLI. Укажи его в лаунчере (Настройки).',
                              'WolvenKit CLI not found. Set it in the launcher (Settings).'))
    r = subprocess.run([exe, *args, '-v', 'Minimal'], capture_output=True, text=True, encoding='utf-8',
                       errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode != 0:
        raise RuntimeError('WolvenKit: ' + (r.stdout + r.stderr)[-600:])
    return r.stdout


def serialize(mesh_path, out_dir):
    """.mesh -> .mesh.json рядом в out_dir."""
    wk('convert', 'serialize', mesh_path, '-o', out_dir)
    j = os.path.join(out_dir, os.path.basename(mesh_path) + '.json')
    if not os.path.isfile(j):
        raise RuntimeError(tr('WolvenKit не смог прочитать ', 'WolvenKit could not read ') + os.path.basename(mesh_path))
    return j


def deserialize(json_path, out_dir):
    """.mesh.json -> .mesh в out_dir. WolvenKit при ошибке разбора возвращает код 0 и молчит,
    поэтому проверяем, что файл правда появился, и показываем его сообщение об ошибке."""
    name = os.path.basename(json_path)[:-5]
    dst = os.path.join(out_dir, name)
    if os.path.isfile(dst):
        os.remove(dst)
    out = wk('convert', 'deserialize', json_path, '-o', out_dir)
    if os.path.isfile(dst):
        return dst
    log('WolvenKit deserialize output:\n' + out[:2500])
    err = next((l.split(' - ', 1)[-1] for l in out.splitlines() if 'Exception' in l), out.strip()[-200:])
    raise RuntimeError(tr('WolvenKit не собрал ', 'WolvenKit could not build ') + f'{name}: {err[:200]}')


_wk_ver = {}


def wk_version():
    exe = load_config().get('wolvenkit', '')
    if exe not in _wk_ver:
        try:
            r = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=60,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            _wk_ver[exe] = r.stdout.strip().splitlines()[0] if r.stdout.strip() else '?'
        except Exception:
            _wk_ver[exe] = '?'
    return _wk_ver[exe]


def files_regex(paths):
    return '(' + '|'.join(re.escape(p) for p in paths) + ')$'


def extract(archives, out_dir, paths):
    """Достаёт из архивов файлы по путям {ключ: путь в игре} -> {ключ: файл на диске}."""
    os.makedirs(out_dir, exist_ok=True)
    wk('unbundle', *archives, '-o', out_dir, '-r', files_regex(paths.values()))
    return {k: os.path.join(out_dir, p) for k, p in paths.items() if os.path.isfile(lp(os.path.join(out_dir, p)))}


def to_json(files, out_dir):
    """{ключ: .mesh} -> {ключ: .mesh.json} в out_dir (имена по ключам)."""
    import shutil
    os.makedirs(out_dir, exist_ok=True)
    res = {}
    for k, f in files.items():
        dst = os.path.join(out_dir, k + '.mesh')
        if os.path.abspath(f) != os.path.abspath(dst):
            shutil.copyfile(lp(f), lp(dst))
        res[k] = serialize(dst, out_dir)
    return res


def vanilla_jsons(progress=None):
    """JSON моделей обычного тела и татуировок из игры. Исходные .mesh храним в кэше, а JSON
    пересоздаём, если сменилась версия WolvenKit: JSON одной версии другая читать не обязана."""
    import shutil
    allf = {**BODY_FILES, **TATTOO_FILES}
    raw = os.path.join(CACHE, 'vanilla_mesh')
    jdir = os.path.join(CACHE, 'vanilla_json')
    vfile = os.path.join(jdir, 'wolvenkit_version.txt')
    have = {k: os.path.join(jdir, k + '.mesh.json') for k in allf}
    ver = wk_version()
    if all(os.path.isfile(p) for p in have.values()) and os.path.isfile(vfile) \
            and open(vfile, encoding='utf-8').read().strip() == ver:
        return have
    meshes = {k: os.path.join(raw, k + '.mesh') for k in allf}
    if not all(os.path.isfile(p) for p in meshes.values()):
        if progress:
            progress(5, tr('Достаю обычное тело из файлов игры (один раз)...',
                           'Extracting the vanilla body from the game files (once)...'))
        tmp = work_dir('game')
        files = extract(game_archive_dirs(), tmp, allf)
        missing = set(BODY_FILES) - set(files)
        if missing:
            raise RuntimeError(tr('В файлах игры не нашлись модели тела: ', 'Body models not found in the game files: ') +
                               ', '.join(sorted(missing)) +
                               tr('. Проверь путь к игре в настройках.', '. Check the game path in Settings.'))
        os.makedirs(raw, exist_ok=True)
        for k, f in files.items():
            shutil.copyfile(lp(f), meshes[k])
        rmtree(tmp)
        meshes = {k: p for k, p in meshes.items() if os.path.isfile(p)}
    if progress:
        progress(15, tr('Читаю модели обычного тела...', 'Reading the vanilla body models...'))
    rmtree(jdir)
    res = to_json(meshes, jdir)
    with open(vfile, 'w', encoding='utf-8') as f:
        f.write(ver)
    return res


def game_archive_dirs(cfg=None):
    cfg = cfg or load_config()
    g = cfg.get('game', '')
    dirs = [os.path.join(g, 'archive', 'pc', d) for d in ('content', 'ep1')]
    dirs = [d for d in dirs if os.path.isdir(d)]
    if not dirs:
        raise RuntimeError(tr('Не найдена папка игры. Укажи её в лаунчере (Настройки).',
                              'Game folder not found. Set it in the launcher (Settings).'))
    return dirs


# ---------- архивы ----------

def fnv1a64(s):
    h = 0xcbf29ce484222325
    for b in s.lower().encode('utf-8'):
        h ^= b
        h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h


def archive_hashes(path):
    """Хэши путей файлов в .archive (без распаковки)."""
    with open(path, 'rb') as f:
        magic, _, ipos, isz = struct.unpack('<4sIQI', f.read(20))
        if magic != b'RDAR':
            return set()
        f.seek(ipos)
        ix = f.read(isz)
    cnt = struct.unpack_from('<I', ix, 16)[0]
    return {struct.unpack_from('<Q', ix, 28 + i * 56)[0] for i in range(cnt)}


# ---------- моды ----------

def _archives_in(folder, depth=5):
    out = []
    base = folder.rstrip('\\/').count(os.sep)
    for dp, dns, fs in os.walk(folder):
        if dp.count(os.sep) - base >= depth:
            dns[:] = []
        for f in fs:
            if f.lower().endswith('.archive'):
                out.append(os.path.join(dp, f))
    return out


def _xls_in(folder):
    out = []
    for dp, _, fs in os.walk(folder):
        out += [os.path.join(dp, f) for f in fs if f.lower().endswith('.xl')]
    return sorted(out)


def scan_mods(cfg=None, include_ours=False):
    """Все моды из папок-источников: [{'name', 'path', 'archives', 'xls', 'source'}].
    Папка источника с подпапками (Vortex, MO2) - мод = подпапка; архивы прямо в папке
    (archive\\pc\\mod игры) - каждый архив отдельный мод."""
    cfg = cfg or load_config()
    mods, seen = [], set()
    for src in cfg.get('sources', []):
        if not os.path.isdir(src):
            continue
        try:
            entries = sorted(os.listdir(src), key=str.lower)
        except OSError:
            continue
        for e in entries:
            full = os.path.join(src, e)
            if os.path.isfile(full) and e.lower().endswith('.archive'):
                stem = os.path.splitext(e)[0]
                xl = [os.path.join(src, x) for x in entries if x.lower().startswith(stem.lower())
                      and x.lower().endswith('.xl')]
                m = {'name': stem, 'path': full, 'archives': [full], 'xls': xl, 'source': src}
            elif os.path.isdir(full):
                arcs = _archives_in(full)
                if not arcs:
                    continue
                m = {'name': e, 'path': full, 'archives': arcs, 'xls': _xls_in(full), 'source': src}
            else:
                continue
            ours = all(os.path.basename(a).lower().startswith(OUR_PREFIXES) for a in m['archives'])
            if (ours and not include_ours) or m['path'].lower() in seen:
                continue
            seen.add(m['path'].lower())
            m['ours'] = ours
            mods.append(m)
    return mods


def body_parts_in(mod):
    """Какие файлы тела (и татуировок) подменяет мод."""
    want = {k: fnv1a64(p) for k, p in {**BODY_FILES, **TATTOO_FILES}.items()}
    found = set()
    for a in mod['archives']:
        try:
            hs = archive_hashes(a)
        except Exception:
            continue
        found |= {k for k, h in want.items() if h in hs}
    return found


def body_tag(mod):
    """Тип тела, который мод регистрирует для ArchiveXL (player: bodyTypes: [ X ])."""
    for x in mod['xls']:
        try:
            text = open(x, encoding='utf-8', errors='ignore').read()
        except OSError:
            continue
        m = re.search(r'bodyTypes\s*:\s*\[([^\]]*)\]', text)
        if m:
            tags = [t.strip(' \'"') for t in m.group(1).split(',') if t.strip(' \'"')]
            if tags:
                return tags[0]
        m = re.search(r'bodyTypes\s*:\s*\n\s*-\s*([A-Za-z0-9_]+)', text)
        if m:
            return m.group(1)
    return ''


# ---------- профили ----------

def list_profiles():
    out = []
    if os.path.isdir(PROFILES):
        for d in sorted(os.listdir(PROFILES)):
            p = os.path.join(PROFILES, d, 'profile.json')
            if os.path.isfile(p) and os.path.isfile(os.path.join(PROFILES, d, 'body.npz')):
                try:
                    out.append(json.load(open(p, encoding='utf-8')))
                except Exception:
                    pass
    return out


def profile_dir(pid):
    return os.path.join(PROFILES, pid)
