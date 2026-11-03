# Refit Studio - мастерская в Blender: подгонка одежды Cyberpunk 2077 под выбранное тело.
# Запускает лаунчер: blender --python studio.py -- --profile <id профиля тела>
# Одежда загружается в координатах игры: где она стоит относительно тела здесь, там будет и в игре.
# Менять можно только положение точек, не их количество.
import bpy, os, sys, re, json, shutil, zipfile, traceback, datetime
import numpy as np
from bpy.app.handlers import persistent

CORE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CORE)
import common as C
import fit
from meshpos import MeshPos, has_geometry

tr = C.tr
CLOTH = tr('Одежда', 'Clothing')
CLOTH_NAMES = ('Одежда', 'Clothing')  # сохранённые работы могли быть сделаны на другом языке
TAB = tr('Рефит', 'Refit')
TATTOO_OBJ = tr('Татуировка', 'Tattoo')
SKIN = (0.85, 0.66, 0.55, 1)
CLOTH_COLOR = (0.35, 0.45, 0.9, 1)
TATTOO_COLOR = (0.1, 0.8, 0.8, 1)

state = {'mods': [], 'meshes': [], 'mod': None, 'mesh': None, 'raw': None, 'json': None,
         'orig': None, 'ranges': None, 'base': None, 'tris': None, 'saves': []}
B = {'body': None}  # загруженный профиль тела


def arg(name):
    a = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None


def body():
    if B['body'] is None:
        pid = arg('--profile') or C.load_config().get('profile')
        if not pid or not os.path.isdir(C.profile_dir(pid)):
            raise RuntimeError(tr('Не выбрано тело. Закрой Blender и выбери тело в лаунчере.',
                                  'No body selected. Close Blender and choose a body in the launcher.'))
        B['body'] = fit.Body(C.profile_dir(pid))
    return B['body']


def body_label():
    try:
        return C.clean_mod_name(body().info['name'])
    except Exception:
        return '?'


def report(op, msg, level='INFO'):
    bpy.context.scene.refit_status = msg
    op.report({level}, msg)


class Progress:
    """Проценты у курсора и текст в строке состояния, чтобы было видно, что Blender работает."""

    def __init__(self, title):
        self.title = title
        self.wm = bpy.context.window_manager
        self.wm.progress_begin(0, 100)

    def __call__(self, pct, text=''):
        self.wm.progress_update(int(pct))
        try:
            bpy.context.workspace.status_text_set(f'{self.title}: {text} ({int(pct)}%)')
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        except Exception:
            pass

    def done(self):
        self.wm.progress_end()
        try:
            bpy.context.workspace.status_text_set(None)
        except Exception:
            pass


# ---------- сцена ----------

def set_color_attr(ob, colors):
    me = ob.data
    attr = me.color_attributes.get('fit') or me.color_attributes.new('fit', 'FLOAT_COLOR', 'POINT')
    attr.data.foreach_set('color', np.asarray(colors, np.float32).ravel())
    me.color_attributes.active_color = attr
    me.update()


def build_object(name, pos, tris, coll, color):
    me = bpy.data.meshes.new(name)
    me.vertices.add(len(pos))
    me.vertices.foreach_set('co', np.asarray(pos, np.float32).ravel())
    tris = np.asarray(tris, np.int64)
    tris = tris[(tris < len(pos)).all(1)]
    me.loops.add(len(tris) * 3)
    me.polygons.add(len(tris))
    me.loops.foreach_set('vertex_index', tris.astype(np.int32).ravel())
    me.polygons.foreach_set('loop_start', np.arange(0, len(tris) * 3, 3, dtype=np.int32))
    me.polygons.foreach_set('use_smooth', np.ones(len(tris), bool))
    me.update(calc_edges=True)
    me.validate(clean_customdata=False)
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    set_color_attr(ob, np.tile(color, (len(pos), 1)))
    return ob


def setup_view():
    """Вид спереди на персонажа (он смотрит вдоль +Y), цвет по точкам, наша вкладка справа.
    Окна перебираем явно: из таймера у Blender нет «текущего» экрана."""
    from mathutils import Euler
    from math import radians
    screens = [w.screen for w in bpy.context.window_manager.windows if w.screen]
    for area in (a for s in screens for a in s.areas):
        if area.type == 'VIEW_3D':
            sp = area.spaces.active
            sp.shading.type = 'SOLID'
            sp.shading.color_type = 'VERTEX'
            r3 = sp.region_3d
            r3.view_perspective = 'PERSP'
            r3.view_rotation = Euler((radians(82), 0, radians(160))).to_quaternion()
            r3.view_location = (0, 0, 1.0)
            r3.view_distance = 3.0
            sp.show_region_ui = True
            for region in area.regions:
                if region.type == 'UI':
                    try:
                        region.active_panel_category = TAB  # сразу наша вкладка справа
                    except Exception:
                        pass


BODY_COLL = tr('Тело', 'Body')
PART_LABELS = {'torso': tr('Тело', 'Body'), 'arm_l': tr('Рука L', 'Arm L'), 'arm_r': tr('Рука R', 'Arm R'),
               'feet': tr('Ступни', 'Feet')}


def setup_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)
    coll = bpy.data.collections.new(BODY_COLL)
    bpy.context.scene.collection.children.link(coll)
    b = body()
    for part, pos, tris in b.display:
        if part == 'feet':
            continue  # ступни дублируют низ тела (это вариант под обувь), в сцене не нужны
        build_object(PART_LABELS.get(part, part), pos, tris, coll, SKIN).hide_select = True
    bpy.context.scene.refit_body = body_label()
    show_tattoo(bpy.context.scene.refit_tattoo)
    setup_view()


def tattoo_items(self, context):
    items = [('none', tr('Без татуировки', 'No tattoo'), tr('Татуировка не учитывается', 'Tattoo is ignored'))]
    try:
        for key, has_body in body().tattoos():
            ok = has_body or tattoo_refit(key) is not None
            items.append((key, tr('Татуировка ', 'Tattoo ') + key[-2:] +
                          ('' if ok else tr(' (без рефита под тело)', ' (not refitted for this body)')),
                          tr('Есть рефит под это тело: одежда подгоняется полностью',
                             'Refitted for this body: clothing is fitted fully') if ok
                          else tr('Рефита под тело нет: одежда не уйдёт под её оболочку (но и грудь там не сузится)',
                                  'Not refitted for this body: clothing stays above its shell '
                                  '(but will not get slimmer there either)')))
    except Exception:
        C.log('tattoo list:\n' + traceback.format_exc())
    return C_keep('tattoos', items)


_enum_keep = {}


def C_keep(key, items):
    # Blender не копирует строки из динамических списков меню: держим список у себя,
    # иначе Python освободит строки и в меню будет "мусор".
    _enum_keep[key] = items
    return items


def tattoo_refit(key):
    """Позиции татуировки после нашего рефита (кнопка «Рефит татуировок»), если он был."""
    if 'tattoo_refit' not in B:
        p = os.path.join(body().folder, 'tattoo_refit.npz')
        B['tattoo_refit'] = dict(np.load(p)) if os.path.isfile(p) else {}
    return B['tattoo_refit'].get(key)


def tattoo_shape(key):
    """(позиции, нормали, треугольники) татуировки на этом теле и нужен ли режим оболочки."""
    b = body()
    pos, n, tris = b.tattoo_mesh(key)
    has_body = dict(b.tattoos()).get(key)
    if not has_body:
        r = tattoo_refit(key)
        if r is not None:
            return r, fit.vertex_normals(r, tris), tris, False
    return pos, n, tris, not has_body


def show_tattoo(key):
    old = bpy.data.objects.get(TATTOO_OBJ)
    if old:
        bpy.data.objects.remove(old, do_unlink=True)
    if not key or key == 'none':
        return
    pos, _, tris, _ = tattoo_shape(key)
    coll = bpy.data.collections.get(BODY_COLL) or bpy.context.scene.collection
    ob = build_object(TATTOO_OBJ, pos, tris, coll, TATTOO_COLOR)
    ob.hide_select = True
    ob.display_type = 'WIRE'


def on_tattoo(self, context):
    try:
        show_tattoo(self.refit_tattoo)
    except Exception as e:
        C.log('tattoo: ' + str(e))


def shell_key(context):
    """Татуировка, под оболочку которой нельзя уводить ткань (нет рефита под тело)."""
    key = context.scene.refit_tattoo
    if key and key != 'none':
        return key if tattoo_shape(key)[3] else None
    return None


def tattoo_obstacles(context):
    """Оболочка выбранной татуировки как препятствие для ткани: [(на этом теле, на обычном теле)].
    Под неё ткань не уводим, иначе в игре рисунок проступит сквозь одежду."""
    key = context.scene.refit_tattoo
    if not key or key == 'none' or fit.BVHTree is None:
        return []
    cache = B.setdefault('obstacles', {})
    if key not in cache:
        b = body()
        tp, _, tt, _ = tattoo_shape(key)
        vp, vt = b.z[key + '_van'], b.z[key + '_van_tris']
        cache[key] = fit.tattoo_obstacles(b, tp, tt, vp, vt)
    return cache[key]


# ---------- выбор мода и вещи ----------

def scan_mods():
    state['mods'] = C.scan_mods()
    return state['mods']


def mod_items(self, context):
    return C_keep('mods', [(str(i), m['name'], m['path']) for i, m in enumerate(state['mods'] or scan_mods())])


def looks_female(p):
    p = p.lower().replace('\\', '/')
    name = p.rsplit('/', 1)[-1]
    return (name.startswith('w_') or '_w_' in name or 'pwa' in p or name.startswith('wa_')
            or '_wa_' in name or 'female' in p or '/w/' in p or 'fem' in name)


def looks_male(p):
    p = p.lower().replace('\\', '/')
    name = p.rsplit('/', 1)[-1]
    return (name.startswith('m_') or '_m_' in name or 'pma' in p or name.startswith('ma_')
            or '_ma_' in name or '/male' in p or '/m/' in p) and not looks_female(p)


# Версии одежды под другие тела: авто-подгонка считает, что вещь сделана под обычное тело,
# поэтому брать надо версию под обычное тело (base_body / vanilla).
OTHER_BODIES = ('angel', 'ebb', 'ebbp', 'ebbrb', 'ebbprb', 'vtk', 'evb', 'eve', 'vhd', 'rb', 'gymfiend',
                'lush', 'solo', 'bigboobs', 'flat', 'small', 'big', 'songbird', 'spawn0', 'elegy',
                'freyja', 'hyst_ebb', 'hyst_rb', 'atlas', 'adonis', 'valentine')
BODY_NAMES = {'angel': 'Angel', 'ebb': 'EBB', 'ebbp': 'EBB+', 'ebbrb': 'EBB RB', 'ebbprb': 'EBB+ RB',
              'vtk': 'VTK', 'evb': 'EVB', 'eve': 'EVE', 'vhd': 'VHD', 'rb': 'RB', 'gymfiend': 'Gymfiend',
              'lush': 'Lush', 'solo': 'Solo', 'bigboobs': tr('большая грудь', 'big breasts'),
              'flat': tr('плоская', 'flat'), 'small': tr('маленькая', 'small'), 'big': tr('большая', 'big'), 'songbird': 'Songbird', 'spawn0': 'Spawn0',
              'elegy': 'Elegy', 'freyja': 'Freyja'}
NOISE = {'w', 'm', 'wa', 'ma', 'pwa', 'pma', 'base', 'body', 'vanilla', 'mesh', 'meshes', 'xl', 'main',
         'female', 'male', 'fem', 'fpp', 'tpp', 'lod', 'lod0', 'lod1', 'items', 'item', 'clothing', 'clothes',
         'characters', 'common', 'player', 'archive', 'mod', 'mods', 'garment', 't0', 't1', 't2', 'a0', 's0',
         'l0', 'h0', 'g0', 'b0'}


def body_kind(path):
    name = os.path.splitext(os.path.basename(path))[0].lower()
    tokens = set(re.split(r'[_\-.]+', name))
    if 'base' in tokens or 'vanilla' in tokens or 'base_body' in name:
        return 'base'
    tag = (body().info.get('tag') or '').lower()
    if tag and tag in name:
        return 'this'
    if tokens & set(OTHER_BODIES):
        return 'other'
    return ''


def friendly(rel):
    """'base\\peachu\\gothic_skirt\\xl_meshes\\peachu_gothic_skirt_w_base_body.mesh' -> 'Gothic skirt — обычное тело'."""
    parts = rel.replace('/', '\\').lower().split('\\')
    name = os.path.splitext(parts[-1])[0]
    tokens = [t for t in re.split(r'[_\-.]+', name) if t]
    bodies = [BODY_NAMES[t] for t in tokens if t in BODY_NAMES]
    words = [t for t in tokens if t not in NOISE and t not in BODY_NAMES and not re.fullmatch(r'\d{3,}', t)]
    folders = [f for f in parts[:-1] if f not in NOISE and not f.endswith('meshes')]
    if words and len(words) > 1 and any(words[0] == f.split('_')[0] for f in folders[:2]):
        words = words[1:]
    if not words and folders:
        words = re.split(r'[_\-.]+', folders[-1])
    title = ' '.join(words).strip().capitalize() or name
    kind = body_kind(rel)
    if kind == 'base':
        title += tr(' — обычное тело', ' — vanilla body')
    elif kind == 'this':
        title += tr(' — уже под это тело', ' — already for this body')
    elif bodies:
        title += ' — ' + ', '.join(dict.fromkeys(bodies))
    return title


def mesh_items(self, context):
    items = []
    for i, m in enumerate(state['meshes']):
        kind = body_kind(m)
        mark = {'base': '★ ', 'other': '✗ ', 'this': '✓ '}.get(kind, '')
        items.append((str(i), mark + friendly(m), m))  # полный путь - во всплывающей подсказке
    return C_keep('meshes', items)


def mod_work(mod):
    return os.path.join(C.WORK, C.short_id(mod['path']))


def open_mod(mod, prog=None):
    """Распаковывает модели мода и составляет список вещей."""
    w = mod_work(mod)
    raw = os.path.join(w, 'raw')
    for d in (raw, raw + '_json'):
        C.rmtree(d)
    os.makedirs(raw)
    for n, a in enumerate(mod['archives']):
        if prog:
            prog(10 + 60 * n / len(mod['archives']), tr('распаковка ', 'unpacking ') + os.path.basename(a))
        C.wk('unbundle', a, '-o', raw, '-r', r'\.(mesh|ent|app)$')
    # .ent/.app хранят пути сжатыми: переводим в JSON, чтобы найти шаблоны вида {gender}_{body}.mesh
    if prog:
        prog(75, tr('чтение .ent/.app', 'reading .ent/.app'))
    try:
        os.makedirs(raw + '_json', exist_ok=True)
        for pat in ('*.ent', '*.app'):
            C.wk('convert', 'serialize', raw, '-o', raw + '_json', '-w', pat)
    except Exception:
        pass
    meshes = []
    for dp, _, fs in os.walk(raw):
        for f in fs:
            full = os.path.join(dp, f)
            # Файлы-хранилища материалов (обычно в ...\materials\..., пара КБ) - без 3D-модели
            if f.lower().endswith('.mesh') and os.path.getsize(full) > 16384 \
                    and '\\materials\\' not in full.lower():
                rel = os.path.relpath(full, raw)
                if not looks_male(rel):
                    meshes.append(rel)
    meshes.sort(key=lambda m: (not looks_female(m), body_kind(m) != 'base', body_kind(m) == 'other', m.lower()))
    state.update(mod=mod, raw=raw, meshes=meshes)
    json.dump({'mod': mod}, open(os.path.join(w, 'mod.json'), 'w', encoding='utf-8'), ensure_ascii=False)


class REFIT_OT_choose_mod(bpy.types.Operator):
    bl_idname = 'refit.choose_mod'
    bl_label = tr('Выбрать мод одежды', 'Choose clothing mod')
    bl_description = tr('Поиск по модам из папок, указанных в лаунчере (Vortex, MO2, папка игры)',
                        'Search the mods in the folders set in the launcher (Vortex, MO2, game folder)')
    bl_property = 'mod'
    mod: bpy.props.EnumProperty(items=mod_items)

    def invoke(self, context, event):
        scan_mods()
        if not state['mods']:
            report(self, tr('Модов не найдено. Проверь папки с модами в лаунчере.',
                            'No mods found. Check the mod folders in the launcher.'), 'WARNING')
            return {'CANCELLED'}
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        mod = state['mods'][int(self.mod)]
        prog = Progress(tr('Открываю мод', 'Opening mod'))
        try:
            open_mod(mod, prog)
        finally:
            prog.done()
        context.scene.refit_mod = mod['name']
        context.scene.refit_mesh = ''
        report(self, tr(f'Мод: {C.clean_mod_name(mod["name"])}. Вещей: {len(state["meshes"])}. '
                        '★ - под обычное тело (бери их), ✗ - под другие тела, ✓ - уже под твоё тело.',
                        f'Mod: {C.clean_mod_name(mod["name"])}. Items: {len(state["meshes"])}. '
                        '★ - for the vanilla body (use these), ✗ - for other bodies, ✓ - already for your body.'))
        return {'FINISHED'}


def load_item(context, rel, json_path, orig=None):
    """Создаёт объект одежды из JSON меша. orig - исходные позиции (если вещь из сохранения)."""
    pos, tris, ranges = MeshPos(json_path).lod0()
    old = cloth_obj()
    if old:
        bpy.data.objects.remove(old, do_unlink=True)
    ob = build_object(CLOTH, pos, tris, context.scene.collection, CLOTH_COLOR)
    state.update(mesh=rel, json=json_path, orig=(orig if orig is not None else pos.copy()), ranges=ranges, base=None,
                 tris=tris)
    context.scene.refit_mesh = rel
    bpy.ops.object.select_all(action='DESELECT')
    ob.select_set(True)
    context.view_layer.objects.active = ob
    return ob, pos


class REFIT_OT_choose_mesh(bpy.types.Operator):
    bl_idname = 'refit.choose_mesh'
    bl_label = tr('Выбрать вещь', 'Choose item')
    bl_property = 'mesh'
    mesh: bpy.props.EnumProperty(items=mesh_items)

    def invoke(self, context, event):
        if not state['meshes']:
            report(self, tr('Сначала выбери мод одежды', 'Choose a clothing mod first'), 'WARNING')
            return {'CANCELLED'}
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        rel = state['meshes'][int(self.mesh)]
        edit = os.path.join(mod_work(state['mod']), 'edit')
        C.rmtree(edit)
        os.makedirs(edit)
        dst = os.path.join(edit, 'item.mesh')
        shutil.copyfile(C.lp(os.path.join(state['raw'], rel)), dst)
        jpath = C.serialize(dst, edit)
        if not has_geometry(jpath):
            report(self, os.path.basename(rel) + tr(' - это файл материалов без 3D-модели. Выбери другую вещь.',
                                                    ' is a materials file without a 3D model. Choose another item.'),
                   'WARNING')
            return {'CANCELLED'}
        ob, pos = load_item(context, rel, jpath)
        kind = body_kind(rel)
        if kind == 'other':
            report(self, tr(f'Внимание: {os.path.basename(rel)} - версия под другое тело. Авто-подгонка рассчитана '
                            'на версию под обычное тело (★), выбери лучше её.',
                            f'Note: {os.path.basename(rel)} is made for another body. Auto fit expects '
                            'the vanilla body version (★) - better pick that one.'), 'WARNING')
        elif kind == 'this':
            report(self, tr(f'{os.path.basename(rel)} уже сделана автором под твоё тело - авто-подгонка не нужна, '
                            'можно только поправить руками.',
                            f'{os.path.basename(rel)} is already made by the author for your body - no auto fit '
                            'needed, you can only touch it up by hand.'), 'WARNING')
        else:
            report(self, tr(f'Загружено: {os.path.basename(rel)} ({len(pos)} точек). Можно «Авто-подгонку» или сразу лепить.',
                            f'Loaded: {os.path.basename(rel)} ({len(pos)} points). Press "Auto fit" or start sculpting.'))
        return {'FINISHED'}


# ---------- правка ----------

def cloth_obj():
    for name in (CLOTH,) + CLOTH_NAMES:
        ob = bpy.data.objects.get(name)
        if ob:
            return ob
    return None


def get_pos(ob):
    """Позиции точек в мире: если вещь двигали целиком (G/R/S в режиме объекта), это тоже учитывается."""
    if ob.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    a = np.empty(len(ob.data.vertices) * 3)
    ob.data.vertices.foreach_get('co', a)
    p = a.reshape(-1, 3)
    mw = np.array(ob.matrix_world)
    return p @ mw[:3, :3].T + mw[:3, 3]


def set_pos(ob, pos):
    """Записывает позиции в мире и сбрасывает перемещение объекта, чтобы оно не применилось дважды."""
    if ob.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    ob.matrix_world.identity()
    ob.data.vertices.foreach_set('co', np.asarray(pos, np.float64).ravel())
    ob.data.update()


def fit_points(points, context, tris=None):
    """Авто-подгонка с учётом выбранной татуировки. tris - треугольники вещи (сглаживание и натяжение)."""
    shell = shell_key(context)
    r = fit.autofit(points, body(), shell, tris)
    if not shell:
        r = fit.conform(r, points, body())
    return fit.settle(r, points, body(), tris, shells=tattoo_obstacles(context))


class REFIT_OT_autofit(bpy.types.Operator):
    bl_idname = 'refit.autofit'
    bl_label = tr('Авто-подгонка', 'Auto fit')
    bl_description = tr('Автоматическая подгонка под выбранное тело (туловище, ноги, руки; свободные части - плавно)',
                        'Automatic fit to the chosen body (torso, legs, arms; loose parts follow smoothly)')

    def execute(self, context):
        ob = cloth_obj()
        if not ob or state['orig'] is None:
            report(self, tr('Сначала загрузи вещь', 'Load an item first'), 'WARNING')
            return {'CANCELLED'}
        cur = get_pos(ob)
        base = state.get('base')
        if base is None or len(base) != len(cur):
            base = state['orig']
        # Ручные правки = чем текущая форма отличается от последней основы (оригинал или прошлая подгонка).
        # Подгоняем оригинал заново и накладываем правки сверху - так они не теряются.
        # С привязкой к телу правки отбрасываются: вещь садится туда, где её задумал автор.
        edits = np.zeros_like(cur) if context.scene.refit_anchor else cur - base
        prog = Progress(tr('Авто-подгонка', 'Auto fit'))
        try:
            prog(10, tr('считаю', 'computing'))
            shell = shell_key(context)
            tris = state.get('tris')
            fitted = fit.autofit(state['orig'], body(), shell, tris)
            result = fitted + edits
            if not shell:
                prog(40, tr('прилегание', 'fitting to skin'))
                # Ткань у тела ложится на новое тело там, где вещь стоит сейчас (с зазором как у автора)
                result = fit.conform(result, state['orig'], body())
            prog(60, tr('натяжение ткани', 'settling the cloth'))
            # Ткань держит форму автора (с ручными правками) и не уходит под кожу и под татуировку
            result = fit.settle(result, state['orig'] + edits, body(), tris, shells=tattoo_obstacles(context))
        finally:
            prog.done()
        set_pos(ob, result)
        state['base'] = result - edits
        set_color_attr(ob, np.tile(CLOTH_COLOR, (len(state['orig']), 1)))
        moved = float(np.abs(edits).max()) if len(edits) else 0
        report(self, tr('Авто-подгонка готова', 'Auto fit done') +
               (tr(' (привязка к телу: вещь на своём месте)', ' (snap to body: item is back in place)')
                if context.scene.refit_anchor else
                tr(', твои правки сохранены', ', your edits are kept') if moved > 1e-5 else '') +
               tr('. «Проверить посадку» покажет, где доработать.', '. "Check fit" shows what to touch up.'))
        return {'FINISHED'}


class REFIT_OT_reset(bpy.types.Operator):
    bl_idname = 'refit.reset'
    bl_label = tr('Вернуть оригинал', 'Reset to original')

    def execute(self, context):
        ob = cloth_obj()
        if ob and state['orig'] is not None:
            set_pos(ob, state['orig'])
            state['base'] = None
            set_color_attr(ob, np.tile(CLOTH_COLOR, (len(state['orig']), 1)))
            report(self, tr('Вещь возвращена к оригиналу автора.', "Item reset to the author's original."))
        return {'FINISHED'}


class REFIT_OT_sculpt(bpy.types.Operator):
    bl_idname = 'refit.sculpt'
    bl_label = tr('Лепить', 'Sculpt')
    bl_description = tr('Режим лепки: Grab - тянуть, Smooth - сгладить, Inflate - надуть. Симметрия по X включена',
                        'Sculpt mode: Grab - pull, Smooth - smooth, Inflate - puff up. X symmetry is on')

    def execute(self, context):
        ob = cloth_obj()
        if not ob:
            report(self, tr('Сначала загрузи вещь', 'Load an item first'), 'WARNING')
            return {'CANCELLED'}
        context.view_layer.objects.active = ob
        ob.select_set(True)
        bpy.ops.object.mode_set(mode='SCULPT')
        ob.use_mesh_mirror_x = True
        for call in (lambda: bpy.ops.brush.asset_activate(
                         asset_library_type='ESSENTIALS',
                         relative_asset_identifier='brushes\\essentials_brushes-mesh_sculpt.blend\\Brush\\Grab'),
                     lambda: bpy.ops.wm.tool_set_by_id(name='builtin_brush.Grab')):
            try:
                call()
                break
            except Exception:
                pass
        report(self, tr('Лепка: Grab - тянуть, Smooth - сгладить, Inflate - надуть. F - размер, Shift+F - сила.',
                        'Sculpt: Grab - pull, Smooth - smooth, Inflate - puff up. F - size, Shift+F - strength.'))
        return {'FINISHED'}


# ---------- проверка посадки ----------

def outward(pos, tris, index, normals):
    """Нормали оболочки, развёрнутые наружу (по нормалям кожи под ней)."""
    n = fit.vertex_normals(pos, tris)
    idx, _ = index.query(pos, 1)
    flip = (n * normals[idx[:, 0]]).sum(1) < 0
    n[flip] *= -1
    return n


class REFIT_OT_check(bpy.types.Operator):
    bl_idname = 'refit.check'
    bl_label = tr('Проверить посадку', 'Check fit')
    bl_description = tr('Красный - ткань под кожей или татуировкой, жёлтый - висит заметно дальше, чем задумал автор',
                        'Red - cloth under the skin or a tattoo, yellow - hangs noticeably further than the author intended')

    def execute(self, context):
        ob = cloth_obj()
        if not ob or state['orig'] is None:
            report(self, tr('Сначала загрузи вещь', 'Load an item first'), 'WARNING')
            return {'CANCELLED'}
        cur = get_pos(ob)
        b = body()
        prog = Progress(tr('Проверка посадки', 'Checking fit'))
        try:
            prog(20, tr('зазоры', 'gaps'))
            # Зазор до гладкой плоскости соседних точек тела. Сравнение с отдельными точками
            # давало ложное "красное" в изгибах: туда попадали точки с другой стороны тела.
            if fit.BVHTree is not None:
                # точный зазор до поверхности (а не до ближайших точек тела)
                gap, _ = b.surf_gap(cur, 0.04)
                gap0, _ = b.van_gap(state['orig'], 0.04)
            else:
                gap, _, d1 = fit.plane_gap(cur, b.surf_index, b.surf, b.n_surf)
                gap0, _, d0 = fit.plane_gap(state['orig'], b.van_index, b.van, b.n_van)
                gap[d1 > 0.04] = np.inf
                gap0[d0 > 0.04] = np.inf
            hard, hard0 = gap, gap0  # препятствия: кожа и (если выбрана) татуировка
            key = context.scene.refit_tattoo
            if key and key != 'none':
                prog(60, tr('татуировка', 'tattoo'))
                # Татуировка поверх кожи - тоже препятствие. Но и у автора ткань местами лежит под её
                # оболочкой (в игре так и задумано), такие места не считаем ошибкой подгонки.
                if fit.BVHTree is not None:
                    new_sh, van_sh = tattoo_obstacles(context)[0]
                    gt, _ = new_sh.gap(cur, 0.01)
                    gt0, _ = van_sh.gap(state['orig'], 0.01)
                    hard = np.minimum(gap, gt)
                    hard0 = np.minimum(gap0, gt0)
                else:
                    tp, _, tt, _ = tattoo_shape(key)
                    tn = outward(tp, tt, b.surf_index, b.n_surf)
                    gt, _, dt = fit.plane_gap(cur, fit.Index(tp), tp, tn)
                    hard = np.where(dt < 0.01, np.minimum(gap, gt), gap)
                    vp = b.z[key + '_van']
                    vn = outward(vp, b.z[key + '_van_tris'], b.van_index, b.n_van)
                    gt0, _, dt0 = fit.plane_gap(state['orig'], fit.Index(vp), vp, vn)
                    hard0 = np.where(dt0 < 0.01, np.minimum(gap0, gt0), gap0)
        finally:
            prog.done()
        with np.errstate(invalid='ignore'):
            inside = np.isfinite(hard) & (hard < -0.0005) & ~(np.isfinite(hard0) & (hard0 < -0.0005))
            ref = np.maximum(gap0, 0)
            base = state.get('base')
            if fit.BVHTree is not None and base is not None and len(base) == len(cur):
                # После авто-подгонки ткань местами отходит от тела нарочно: под грудью и под ягодицами
                # она натягивается мостиком. Жёлтым - только то, что висит дальше, чем положила подгонка.
                gb, _ = b.surf_gap(base, 0.04)
                ref = np.where(np.isfinite(gb), np.maximum(ref, gb), ref)
            loose = np.isfinite(gap) & np.isfinite(gap0) & (gap - ref > 0.006)
        col = np.tile(CLOTH_COLOR, (len(cur), 1))
        col[loose] = (1.0, 0.85, 0.1, 1)
        col[inside] = (1.0, 0.1, 0.1, 1)
        set_color_attr(ob, col)
        setup_view()
        report(self, tr(f'Под кожей или татуировкой (красное): {int(inside.sum())} точек, висит (жёлтое): {int(loose.sum())}. '
                        'После правки нажми ещё раз.',
                        f'Under the skin or a tattoo (red): {int(inside.sum())} points, hanging (yellow): {int(loose.sum())}. '
                        'Press again after editing.'))
        return {'FINISHED'}


# ---------- экспорт ----------

def dynamic_name(raw, rel):
    """Если мод выбирает модель по шаблону с {body}, возвращает путь модели для типа нашего тела."""
    tag = (body().info.get('tag') or '').lower()
    if not tag:
        return None
    rel_l = rel.replace('/', '\\').lower()
    folder, fname = os.path.split(rel_l)
    jdir = raw + '_json'
    if not os.path.isdir(jdir):
        return None
    for dp, _, fs in os.walk(jdir):
        for f in fs:
            if not f.lower().endswith('.json'):
                continue
            text = open(os.path.join(dp, f), encoding='utf-8', errors='ignore').read()
            for pat in set(re.findall(r'"\$value":\s*"([^"]*\{body\}[^"]*\.mesh)"', text)):
                pat = pat.replace('\\\\', '\\').lstrip('*').replace('/', '\\').lower()
                pfolder, pname = os.path.split(pat)
                if pfolder != folder:
                    continue
                rx = '^' + re.sub(r'\\\{[a-z0-9_.]+\\\}', '(.+?)', re.escape(pname)) + '$'
                mt = re.match(rx, fname)
                if not mt:
                    continue
                tokens = re.findall(r'\{([a-z0-9_.]+)\}', pname)
                out = pname
                for tok, val in zip(tokens, mt.groups()):
                    out = out.replace('{' + tok + '}', tag if tok == 'body' else val, 1)
                return os.path.join(folder, out)
    return None


def xl_patches(mod, rel):
    """Ключи resource.patch из .xl мода, в списках которых есть эта модель (подключение материалов)."""
    target = rel.replace('/', '\\').lower()
    found = []
    for x in mod.get('xls', []):
        key = None
        for line in open(x, encoding='utf-8', errors='ignore'):
            s = line.strip()
            if s.endswith(':') and s.lower().endswith('.mesh:'):
                key = s[:-1]
            elif s.startswith('-') and key and s[1:].strip().lower() == target:
                found.append(key)
    return found


def body_short():
    b = body().info
    return C.safe(b.get('tag') or C.clean_mod_name(b['name']), 16)


def build_zip(mod, raw, items, zip_name, prog=None, dynamic=True):
    """items: [(rel, mesh_json_path, [позиции по всем чанкам])] -> один zip с рефитом."""
    out_dir = os.path.join(mod_work(mod) if mod else os.path.join(C.WORK, 'tattoo'), 'out')
    C.rmtree(out_dir)
    arch_name = '#REFIT_' + body_short() + '_' + C.safe(C.clean_mod_name(mod['name']) if mod else 'tattoos', 24)
    pack_root = os.path.join(out_dir, 'p', arch_name)
    xl = {}
    for n, (rel, jpath, results) in enumerate(items):
        if prog:
            prog(60 + 30 * n / max(len(items), 1), tr('сборка ', 'building ') + os.path.basename(rel))
        m = MeshPos(jpath)
        fit.apply_positions(m, results)
        tmp = os.path.join(out_dir, f'i{n}')
        os.makedirs(tmp)
        m.save(os.path.join(tmp, 'item.mesh.json'))
        C.deserialize(os.path.join(tmp, 'item.mesh.json'), tmp)
        rel = rel.replace('/', '\\')
        dyn = dynamic_name(raw, rel) if dynamic else None
        target = dyn or rel
        os.makedirs(C.lp(os.path.join(pack_root, os.path.dirname(target))), exist_ok=True)
        shutil.copyfile(os.path.join(tmp, 'item.mesh'), C.lp(os.path.join(pack_root, target)))
        if dyn:
            # Новая модель под тип нашего тела: материалы подключаем так же, как у оригинала
            for k in xl_patches(mod, rel):
                xl.setdefault(k, []).append(dyn)
    if prog:
        prog(92, tr('упаковка архива', 'packing the archive'))
    C.wk('pack', pack_root, '-o', os.path.join(out_dir, 'p'))
    os.makedirs(C.EXPORTS, exist_ok=True)
    zip_path = os.path.join(C.EXPORTS, C.fs_name(zip_name)[:150] + '.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(pack_root + '.archive', 'archive/pc/mod/' + arch_name + '.archive')
        if xl:
            lines = [f'# Refit Studio: refit for {body_label()}, materials as in the original models',
                     'resource:', '    patch:']
            for k, vals in xl.items():
                lines.append(f'        {k}:')
                lines += [f'           - {v}' for v in vals]
            z.writestr('archive/pc/mod/' + arch_name + '.xl', '\n'.join(lines) + '\n')
    return zip_path


def item_results(jpath, lod0_new, lod0_orig, ranges):
    """Позиции для всех чанков: LOD0 из правки, остальные LOD - перенос правок."""
    m = MeshPos(jpath)
    r = {ci: (a, b) for ci, a, b in ranges}
    out = []
    for ci in range(len(m.chunks)):
        if ci in r:
            out.append(lod0_new[r[ci][0]:r[ci][1]])
        else:
            out.append(fit.transfer(lod0_orig, lod0_new, m.read(ci)))
    return out


def zip_title(item=None):
    t = f'{C.clean_mod_name(body_label())} refit - {C.clean_mod_name(state["mod"]["name"])[:50]}'
    return t + (f' - {item}' if item else tr(' (весь мод)', ' (whole mod)'))


class UserError(Exception):
    """Понятная ошибка для панели (без «подробностей в логе»)."""


def make_item(context, prog):
    """Рефит загруженной вещи -> zip во внутренней папке экспорта."""
    ob = cloth_obj()
    if not ob or state['json'] is None:
        raise UserError(tr('Сначала загрузи вещь', 'Load an item first'))
    new = get_pos(ob)
    if len(new) != len(state['orig']):
        raise UserError(tr('Число точек изменилось (Dyntopo/Remesh/удаление?). Так игра модель не примет. '
                           'Нажми «Вернуть оригинал» и правь только перемещением точек.',
                           'The number of points changed (Dyntopo/Remesh/delete?). The game will not accept that. '
                           'Press "Reset to original" and only move points.'))
    prog(20, 'LOD')
    res = item_results(state['json'], new, state['orig'], state['ranges'])
    item = os.path.splitext(os.path.basename(state['mesh']))[0]
    path = build_zip(state['mod'], state['raw'], [(state['mesh'], state['json'], res)], zip_title(item), prog)
    note = ''
    if author_has_version(state['raw'], state['mesh']):
        note = tr(' (внимание: у автора мода уже есть своя версия этой вещи под твоё тело - рефит её заменит)',
                  " (note: the mod author already has a version of this item for your body - the refit will replace it)")
    return path, tr('вещь ', 'item ') + item + note


def author_has_version(raw, rel):
    """У автора мода уже есть модель этой вещи под тип нашего тела (через шаблон {body})."""
    dyn = dynamic_name(raw, rel.replace('/', os.sep))
    return bool(dyn) and os.path.isfile(C.lp(os.path.join(raw, dyn)))


def make_mod(context, prog):
    """Авто-подгонка всех женских вещей мода под обычное тело (★) -> один zip."""
    if not state['mod']:
        raise UserError(tr('Сначала выбери мод одежды', 'Choose a clothing mod first'))
    picks = [m for m in state['meshes'] if body_kind(m) == 'base' and looks_female(m)]
    if not picks:
        picks = [m for m in state['meshes'] if looks_female(m) and body_kind(m) not in ('other', 'this')]
    if not picks:
        raise UserError(tr('Не нашёл в моде женских вещей под обычное тело. Выбери вещь вручную.',
                           'No female items for the vanilla body found in this mod. Choose an item manually.'))
    # вещи, которые автор уже сделал под это тело, не трогаем: его ручная работа лучше авто-подгонки
    own = [m for m in picks if author_has_version(state['raw'], m)]
    picks = [m for m in picks if m not in own]
    if not picks:
        raise UserError(tr('У автора мода уже есть версии всех вещей под твоё тело - рефит не нужен. '
                           'Если хочешь свою подгонку, выбери вещь и экспортируй её отдельно.',
                           'The mod author already has versions of all items for your body - no refit needed. '
                           'If you want your own fit, choose an item and export it separately.'))
    items = []
    bdir = os.path.join(mod_work(state['mod']), 'batch')
    C.rmtree(bdir)
    os.makedirs(bdir)
    for n, rel in enumerate(picks):
        prog(5 + 55 * n / len(picks), f'{n + 1}/{len(picks)} {os.path.basename(rel)}')
        src = os.path.join(bdir, f'{n}.mesh')
        shutil.copyfile(C.lp(os.path.join(state['raw'], rel)), src)
        jpath = C.serialize(src, bdir)
        if not has_geometry(jpath):
            continue
        m = MeshPos(jpath)
        items.append((rel, jpath, [fit_points(m.read(ci), context, m.triangles(ci)) for ci in range(len(m.chunks))]))
    path = build_zip(state['mod'], state['raw'], items, zip_title(), prog)
    names = ', '.join(os.path.basename(r)[:-5] for r, _, _ in items)
    skip = (tr('; пропущено (у автора уже есть под твоё тело): ', '; skipped (author already has them for your body): ')
            + str(len(own))) if own else ''
    return path, tr('весь мод, вещей: ', 'whole mod, items: ') + f'{len(items)} ({names}){skip}'


def make_tattoos(context, prog):
    """5 татуировок тела из игры, подогнанные под выбранное тело -> zip (заменяет игровые)."""
    b = body()
    keys = [k for k, has_body in b.tattoos() if not has_body]
    if not keys:
        raise UserError(tr('У этого тела уже есть свои татуировки, рефит не нужен.',
                           'This body already has its own tattoos, no refit needed.'))
    src = C.vanilla_jsons()  # пересоздаются, если сменилась версия WolvenKit
    tdir = os.path.join(C.WORK, 'tattoo')
    C.rmtree(tdir)
    os.makedirs(tdir)
    items, lod0 = [], {}
    for n, k in enumerate(keys):
        prog(5 + 50 * n / len(keys), k)
        jpath = os.path.join(tdir, k + '.mesh.json')
        shutil.copyfile(src[k], jpath)
        m = MeshPos(jpath)
        res = [fit.autofit(m.read(ci), b) for ci in range(len(m.chunks))]
        items.append((C.TATTOO_FILES[k], jpath, res))
        lod0[k] = np.concatenate([res[ci] for ci in range(len(m.chunks)) if m.lod_mask(ci) & 1])
    path = build_zip(None, None, items, f'{C.clean_mod_name(body_label())} refit - Body Tattoos', prog,
                     dynamic=False)
    # запоминаем форму татуировок: теперь одежда под них подгоняется полностью
    np.savez_compressed(os.path.join(b.folder, 'tattoo_refit.npz'), **lod0)
    B.pop('tattoo_refit', None)
    B.pop('obstacles', None)  # оболочки татуировок теперь другие
    show_tattoo(context.scene.refit_tattoo)
    return path, tr('татуировки тела; после установки выбирай татуировку в списке - одежда подгоняется полностью',
                    'body tattoos; after installing, pick your tattoo in the list - clothing is then fitted fully')


MAKERS = {'ITEM': make_item, 'MOD': make_mod, 'TATTOO': make_tattoos}


def run_export(context, title):
    prog = Progress(title)
    try:
        return MAKERS[context.scene.refit_scope](context, prog)
    finally:
        prog.done()


def default_zip_name(context):
    scope = context.scene.refit_scope
    if scope == 'TATTOO':
        return f'{C.clean_mod_name(body_label())} refit - Body Tattoos'
    if not state['mod']:
        return 'refit'
    if scope == 'ITEM' and state['mesh']:
        return zip_title(os.path.splitext(os.path.basename(state['mesh']))[0])
    return zip_title()


class REFIT_OT_export_zip(bpy.types.Operator):
    bl_idname = 'refit.export_zip'
    bl_label = tr('Сохранить zip…', 'Save zip…')
    bl_description = tr('Собрать рефит и сохранить zip в папку, которую выберешь',
                        'Build the refit and save the zip to a folder of your choice')
    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default='*.zip', options={'HIDDEN'})

    def invoke(self, context, event):
        self.filepath = os.path.join(C.output_dir(), C.fs_name(default_zip_name(context))[:150] + '.zip')
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        dest = self.filepath or os.path.join(C.output_dir(), C.fs_name(default_zip_name(context))[:150] + '.zip')
        if not dest.lower().endswith('.zip'):
            dest += '.zip'
        path, what = run_export(context, tr('Сохраняю zip', 'Saving zip'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(path, dest)
        cfg = C.load_config()
        if not os.environ.get('REFIT_OUTPUT') and cfg.get('output') != os.path.dirname(dest):
            cfg['output'] = os.path.dirname(dest)  # в следующий раз предложим ту же папку
            C.save_config(cfg)
        report(self, tr('Сохранено', 'Saved') + f' ({what}): {dest}')
        return {'FINISHED'}


class REFIT_OT_export_vortex(bpy.types.Operator):
    bl_idname = 'refit.export_vortex'
    bl_label = tr('В Vortex', 'To Vortex')
    bl_description = tr('Собрать рефит и сразу отдать его Vortex на установку. Vortex ставит мод в игру, '
                        'которая сейчас выбрана в нём, - там должен быть Cyberpunk 2077',
                        'Build the refit and hand it straight to Vortex to install. Vortex installs it into the '
                        'game that is active in Vortex - that must be Cyberpunk 2077')

    def execute(self, context):
        vortex = C.find_vortex()
        if not vortex:
            raise UserError(tr('Не нашёл Vortex. Сохрани zip кнопкой «Сохранить zip…» и перетащи его в Vortex вручную.',
                               'Vortex not found. Save the zip with "Save zip…" and drag it into Vortex manually.'))
        path, what = run_export(context, tr('Отправляю в Vortex', 'Sending to Vortex'))
        C.send_to_vortex(vortex, path)
        report(self, tr(f'Отправлено в Vortex ({what}). Если Vortex спросит - подтверди установку, '
                        'и проверь, что мод включён (Enabled).',
                        f'Sent to Vortex ({what}). If Vortex asks, confirm the install '
                        'and make sure the mod is Enabled.'))
        return {'FINISHED'}


# ---------- сохранение работы ----------

def save_items(self, context):
    return C_keep('saves', [(str(i), s, '') for i, s in enumerate(state['saves'])])


class REFIT_OT_save(bpy.types.Operator):
    bl_idname = 'refit.save'
    bl_label = tr('Сохранить работу', 'Save work')

    def execute(self, context):
        ob = cloth_obj()
        if not ob or state['json'] is None:
            report(self, tr('Нечего сохранять: сначала загрузи вещь', 'Nothing to save: load an item first'), 'WARNING')
            return {'CANCELLED'}
        get_pos(ob)  # выйти из режима лепки
        name = C.safe(C.clean_mod_name(state['mod']['name']), 30) + '__' + \
            C.safe(os.path.splitext(os.path.basename(state['mesh']))[0], 40)
        d = os.path.join(C.SAVES, name)
        os.makedirs(d, exist_ok=True)
        # копия данных вещи: рабочая папка мода перезаписывается при загрузке другой вещи
        if os.path.abspath(state['json']) != os.path.abspath(os.path.join(d, 'item.mesh.json')):
            shutil.copyfile(state['json'], os.path.join(d, 'item.mesh.json'))
        np.save(os.path.join(d, 'orig.npy'), state['orig'])
        if state.get('base') is not None:
            np.save(os.path.join(d, 'base.npy'), state['base'])
        elif os.path.exists(os.path.join(d, 'base.npy')):
            os.remove(os.path.join(d, 'base.npy'))
        sc = context.scene
        sc['refit_save'] = json.dumps({'mod': state['mod'], 'mesh': state['mesh'], 'raw': state['raw'],
                                       'json': os.path.join(d, 'item.mesh.json'), 'orig': os.path.join(d, 'orig.npy'),
                                       'ranges': state['ranges'], 'profile': body().info['id']})
        bpy.ops.wm.save_as_mainfile(filepath=os.path.join(d, 'work.blend'), check_existing=False)
        report(self, tr(f'Сохранено: {name}. Открыть потом - «Открыть сохранённую».',
                        f'Saved: {name}. Open it later with "Open saved".'))
        return {'FINISHED'}


class REFIT_OT_open(bpy.types.Operator):
    bl_idname = 'refit.open'
    bl_label = tr('Открыть сохранённую', 'Open saved')
    bl_property = 'save'
    save: bpy.props.EnumProperty(items=save_items)

    def invoke(self, context, event):
        state['saves'] = sorted(d for d in os.listdir(C.SAVES)
                                if os.path.exists(os.path.join(C.SAVES, d, 'work.blend'))) if os.path.isdir(C.SAVES) else []
        if not state['saves']:
            report(self, tr('Сохранённых работ пока нет', 'No saved work yet'), 'WARNING')
            return {'CANCELLED'}
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        bpy.ops.wm.open_mainfile(filepath=os.path.join(C.SAVES, state['saves'][int(self.save)], 'work.blend'))
        return {'FINISHED'}


@persistent
def restore_state(_):
    """После открытия сохранения восстанавливаем, какая вещь и откуда загружена."""
    sc = bpy.context.scene
    data = sc.get('refit_save')
    if not data:
        return
    d = json.loads(data)
    other_body = bool(d.get('profile')) and d['profile'] != body().info['id']
    if other_body:
        sc.refit_status = tr('Внимание: эта работа сделана под другое тело. Экспорт будет под текущее тело - '
                             'при необходимости переключи тело в лаунчере.',
                             'Note: this work was made for another body. Export will use the current body - '
                             'switch the body in the launcher if needed.')
    base_path = os.path.join(os.path.dirname(d['orig']), 'base.npy')
    state.update(mod=d['mod'], mesh=d['mesh'], raw=d['raw'], json=d['json'],
                 orig=np.load(d['orig']), ranges=[tuple(r) for r in d['ranges']],
                 base=np.load(base_path) if os.path.exists(base_path) else None)
    if d['raw'] and os.path.isdir(d['raw']):
        state['meshes'] = sorted(os.path.relpath(os.path.join(dp, f), d['raw'])
                                 for dp, _, fs in os.walk(d['raw']) for f in fs if f.lower().endswith('.mesh'))
    if not other_body:
        sc.refit_status = tr('Работа открыта: ', 'Work opened: ') + os.path.basename(d['mesh']) + \
            tr('. Продолжай правку.', '. Continue editing.')
    bpy.app.timers.register(lambda: (setup_view(), None)[1], first_interval=0.3)


# ---------- панель ----------

class REFIT_PT_panel(bpy.types.Panel):
    bl_label = 'Refit Studio'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = TAB

    def draw(self, context):
        sc = context.scene
        col = self.layout.column(align=True)
        box = col.box()
        box.label(text=tr('Тело: ', 'Body: ') + (sc.refit_body or '?'), icon='OUTLINER_OB_ARMATURE')
        box.prop(sc, 'refit_tattoo', text='')
        col.separator()
        col.label(text=tr('1. Одежда', '1. Clothing'))
        col.operator('refit.choose_mod', icon='FILE_FOLDER')
        if sc.refit_mod:
            col.label(text=C.clean_mod_name(sc.refit_mod)[:45])
        col.operator('refit.choose_mesh', icon='MOD_CLOTH')
        if sc.refit_mesh:
            col.label(text=friendly(sc.refit_mesh)[:45])
        col.separator()
        col.label(text=tr('2. Подгонка', '2. Fit'))
        col.prop(sc, 'refit_anchor')
        col.operator('refit.autofit', icon='AUTO')
        col.operator('refit.sculpt', icon='SCULPTMODE_HLT')
        col.operator('refit.check', icon='VIEWZOOM')
        col.operator('refit.reset', icon='LOOP_BACK')
        col.separator()
        col.label(text=tr('3. Готово', '3. Export'))
        col.prop(sc, 'refit_scope', expand=True)
        row = col.row(align=True)
        row.scale_y = 1.3
        row.operator('refit.export_zip', icon='EXPORT')
        row.operator('refit.export_vortex', icon='PACKAGE')
        col.separator()
        row = col.row(align=True)
        row.operator('refit.save', text=tr('Сохранить работу', 'Save work'), icon='FILE_TICK')
        row.operator('refit.open', text=tr('Открыть', 'Open'), icon='FILE_BLEND')
        if sc.refit_status:
            box = self.layout.box()
            words, line = sc.refit_status.split(' '), ''
            for w in words:
                if len(line) + len(w) > 32:
                    box.label(text=line)
                    line = ''
                line += w + ' '
            if line.strip():
                box.label(text=line)


classes = (REFIT_OT_choose_mod, REFIT_OT_choose_mesh, REFIT_OT_autofit, REFIT_OT_reset, REFIT_OT_sculpt,
           REFIT_OT_check, REFIT_OT_export_zip, REFIT_OT_export_vortex, REFIT_OT_save, REFIT_OT_open,
           REFIT_PT_panel)


def logged(cls):
    """Любая ошибка кнопки пишется в log.txt и показывается в панели, а не теряется."""
    inner = cls.execute

    def execute(self, context):
        try:
            res = inner(self, context)
            C.log(f'{cls.bl_idname}: {res} | {context.scene.refit_status}')
            return res
        except UserError as e:
            report(self, str(e), 'WARNING')
            return {'CANCELLED'}
        except Exception as e:
            C.log(f'{cls.bl_idname} ERROR:\n{traceback.format_exc()}')
            report(self, tr('Ошибка: ', 'Error: ') + str(e).strip().splitlines()[-1][:250] +
                   tr(' (подробности в log.txt)', ' (details in log.txt)'), 'ERROR')
            return {'CANCELLED'}
    cls.execute = execute
    return cls


def register():
    S = bpy.types.Scene
    S.refit_mod = bpy.props.StringProperty()
    S.refit_mesh = bpy.props.StringProperty()
    S.refit_status = bpy.props.StringProperty()
    S.refit_body = bpy.props.StringProperty()
    S.refit_tattoo = bpy.props.EnumProperty(
        name=tr('Татуировка', 'Tattoo'), items=tattoo_items, update=on_tattoo,
        description=tr('Какая татуировка тела у твоего персонажа. Одежда не будет уходить под её оболочку',
                       "Which body tattoo your character wears. Clothing will not sink under its shell"))
    S.refit_scope = bpy.props.EnumProperty(
        name=tr('Что экспортировать', 'What to export'), default='ITEM',
        items=[('ITEM', tr('Вещь', 'Item'), tr('Только загруженная вещь с твоими правками',
                                              'Only the loaded item with your edits')),
               ('MOD', tr('Весь мод', 'Whole mod'), tr('Авто-подгонка всех вещей мода под обычное тело (★), один zip',
                                                      'Auto fit of all vanilla-body items (★) of the mod, one zip')),
               ('TATTOO', tr('Татуировки', 'Tattoos'), tr('Рефит 5 татуировок тела из игры под выбранное тело',
                                                         'Refit of the 5 game body tattoos for the chosen body'))])
    S.refit_anchor = bpy.props.BoolProperty(
        name=tr('Привязка к телу', 'Snap to body'), default=True,
        description=tr('Вкл: авто-подгонка всегда сажает вещь на её место (ручные сдвиги сбрасываются). '
                       'Выкл: твои сдвиги сохраняются, вещь подгоняется там, куда ты её перетащил',
                       'On: auto fit always puts the item back in its place (manual moves are reset). '
                       'Off: your moves are kept and the item is fitted where you dragged it'))
    os.makedirs(C.WORK, exist_ok=True)
    for c in classes:
        if hasattr(c, 'execute'):
            logged(c)
        bpy.utils.register_class(c)
    bpy.app.handlers.load_post.append(restore_state)


def start():
    try:
        setup_scene()
        bpy.context.scene.refit_status = tr(f'Тело: {body_label()}. Выбери мод одежды, потом вещь со звёздочкой ★.',
                                            f'Body: {body_label()}. Choose a clothing mod, then an item marked ★.')
    except Exception as e:
        C.log('workshop start:\n' + traceback.format_exc())
        bpy.context.scene.refit_status = tr('Ошибка запуска: ', 'Start error: ') + str(e)[:200]
    return None


register()
# сцену строим, когда окно Blender готово (иначе нет экрана для настройки вида)
bpy.app.timers.register(start, first_interval=0.1)


def focus_tab(tries=[0]):
    # Вкладку боковой панели можно переключить только после её первой отрисовки - пробуем несколько раз
    tries[0] += 1
    done = False
    for w in bpy.context.window_manager.windows:
        for area in w.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type == 'UI':
                    try:
                        region.active_panel_category = TAB
                        done = region.active_panel_category == TAB
                        area.tag_redraw()
                    except Exception:
                        pass
    return None if done or tries[0] > 10 else 0.5


bpy.app.timers.register(focus_tab, first_interval=0.8)
