# Сборка профиля тела. Запускается лаунчером в фоне:
#   blender -b --factory-startup --python build_profile.py -- --mod "<папка или .archive мода>"
# Пишет в stdout строки для лаунчера: "@@P <проценты> <текст>", "@@OK <id>", "@@E <ошибка>".
import os, sys, re, json, shutil, datetime, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding='utf-8')  # лаунчер читает вывод в UTF-8
except Exception:
    pass
import numpy as np
import common as C
from meshpos import MeshPos
from fit import vertex_normals


def progress(p, text):
    print(f'@@P {int(p)} {text}', flush=True)


def lod0_data(json_path):
    m = MeshPos(json_path)
    pos, tris, ranges = m.lod0()
    ns = [m.read_dec4(ci, 'PS_Normal')[0] for ci, _, _ in ranges]
    if all(n is not None for n in ns):
        n = np.concatenate(ns)
        n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    else:
        n = vertex_normals(pos, tris)
    return pos, n, tris


def find_mod(path):
    for m in C.scan_mods(include_ours=True):
        if os.path.normcase(m['path']) == os.path.normcase(path):
            return m
    # мод не из папок-источников: берём всё, что лежит по пути
    if os.path.isfile(path):
        d = os.path.dirname(path)
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        xl = [os.path.join(d, x) for x in os.listdir(d) if x.lower().startswith(stem) and x.lower().endswith('.xl')]
        return {'name': os.path.splitext(os.path.basename(path))[0], 'path': path, 'archives': [path], 'xls': xl}
    return {'name': os.path.basename(path.rstrip('\\/')), 'path': path,
            'archives': C._archives_in(path), 'xls': C._xls_in(path)}


def build(mod_path, name=None):
    mod = find_mod(mod_path)
    if not mod['archives']:
        raise RuntimeError(C.tr('В этом моде нет файлов .archive.', 'This mod has no .archive files.'))
    parts = C.body_parts_in(mod)
    if not parts & set(C.BODY_FILES):
        raise RuntimeError(C.tr('Это не мод тела: в нём нет моделей тела V (туловище, руки или ступни). '
                                'Выбери мод, который меняет само тело.',
                                'This is not a body mod: it has no V body models (torso, arms or feet). '
                                'Choose a mod that changes the body itself.'))
    van = C.vanilla_jsons(progress)
    pid = C.safe(mod['name'], 28) + '_' + C.short_id(mod['path'], 6)
    pdir = C.profile_dir(pid)
    tmp = pdir + '_tmp'
    C.rmtree(tmp)
    os.makedirs(tmp)
    # распаковка - в короткую папку в TEMP: пути внутри архивов длинные, а в папке профиля
    # они упирались в лимит Windows (260 символов), и части тела молча пропадали
    work = C.work_dir('body')
    try:
        progress(25, C.tr('Достаю тело из мода...', 'Extracting the body from the mod...'))
        want = {k: p for k, p in {**C.BODY_FILES, **C.TATTOO_FILES}.items() if k in parts}
        files = C.extract(mod['archives'], os.path.join(work, 'raw'), want)
        missing = [k for k in want if k not in files]
        if missing:
            raise RuntimeError(C.tr('Не удалось достать из мода: ', 'Could not extract from the mod: ') +
                               ', '.join(C.part_name(k) for k in missing) +
                               C.tr('. Подробности в log.txt.', '. Details in log.txt.'))
        progress(45, C.tr('Читаю модели тела...', 'Reading the body models...'))
        body = C.to_json(files, os.path.join(work, 'json'))
        info = _compare_and_save(mod, name, pid, van, body, tmp)
    except Exception:
        C.rmtree(tmp)
        raise
    finally:
        C.rmtree(work)
    # старый профиль убираем только когда новый полностью готов
    old = pdir + '_old'
    C.rmtree(old)
    if os.path.isdir(pdir):
        os.replace(pdir, old)
    os.replace(tmp, pdir)
    C.rmtree(old)
    return info


def _compare_and_save(mod, name, pid, van, body, tmp):
    out, info_parts, tattoos = {}, {}, {}
    keys = list(C.BODY_FILES) + list(C.TATTOO_FILES)
    for i, k in enumerate(keys):
        progress(55 + 35 * i / len(keys), C.tr('Сравниваю с обычным телом: ', 'Comparing with the vanilla body: ') + C.part_name(k))
        vp, vn, vt = lod0_data(van[k])
        out[k + '_van'], out[k + '_van_n'], out[k + '_van_tris'] = vp, vn, vt
        if k in C.TATTOO_FILES:
            if k in body:
                out[k + '_body'], out[k + '_body_n'], out[k + '_body_tris'] = lod0_data(body[k])
            tattoos[k] = 'body' if k in body else ''
            continue
        if k in body:
            bp, bn, bt = lod0_data(body[k])
            changed = float(np.abs(bp - vp).max()) if bp.shape == vp.shape else 1.0
            info_parts[k] = 'changed' if changed > 1e-4 else 'same'
        else:
            bp, bn, bt = vp, vn, vt
            info_parts[k] = 'vanilla'
        out[k + '_body'], out[k + '_body_n'], out[k + '_body_tris'] = bp, bn, bt
    if not any(v == 'changed' for v in info_parts.values()):
        raise RuntimeError(C.tr('Тело в этом моде ничем не отличается от обычного - подгонять не к чему.',
                                'The body in this mod is identical to the vanilla body - nothing to refit to.'))
    progress(92, C.tr('Сохраняю профиль...', 'Saving the profile...'))
    np.savez_compressed(os.path.join(tmp, 'body.npz'), **out)
    info = {'id': pid, 'name': name or mod['name'], 'mod_name': mod['name'], 'mod_path': mod['path'],
            'tag': C.body_tag(mod), 'parts': info_parts, 'tattoos': tattoos,
            'created': datetime.datetime.now().isoformat(timespec='seconds'), 'version': 2}
    with open(os.path.join(tmp, 'profile.json'), 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    return info


def main():
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    args = dict(zip(argv[::2], argv[1::2]))
    try:
        info = build(args['--mod'], args.get('--name'))
        C.log(f'profile {info["id"]}: {info["parts"]} tag={info["tag"]}')
        print('@@OK ' + info['id'], flush=True)
    except Exception as e:
        C.log('profile ERROR:\n' + traceback.format_exc())
        print('@@E ' + str(e).replace('\n', ' ')[:500], flush=True)
        sys.stdout.flush()
        os._exit(1)


main()
