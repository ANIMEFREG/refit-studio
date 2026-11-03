# Авто-подгонка одежды под выбранное тело и пересчёт нормалей.
# Тело (профиль) - это разница между обычным телом игры и телом из мода, по частям:
# туловище с ногами, руки, ступни. Одежда сдвигается так же, как сдвинулась кожа под ней.
import json, os
import numpy as np
from knn import Index

try:
    from mathutils.bvhtree import BVHTree
    from mathutils import Vector
except ImportError:  # вне Blender
    BVHTree = None

K = 4                 # сколько ближайших точек тела усредняем у прилегающих частей (вес 1/d²)
INFLATE = 0.0008      # м: прилегающие части чуть отодвигаем от кожи (плавно), чтобы не цепляли её
K_FAR = 64            # у свободных частей (юбки, подолы) - средний сдвиг большой области
NEAR_D = 0.02         # м: до этого расстояния от тела - обычный перенос
FAR_D = 0.08          # м: дальше - полностью средний сдвиг области (плавный переход между)
SHELL_RADIUS = 0.015  # м: татуировка без рефита под тело - вблизи её оболочки тело не "худеет"
SHELL_FADE = 0.01
PARTS = ('torso', 'arm_l', 'arm_r', 'feet')


def vertex_normals(pos, tris):
    """Нормали вершин по треугольникам; точки с одинаковой позицией (швы) получают общую нормаль."""
    tris = tris[(tris < len(pos)).all(1)]
    fn = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
    vn = np.zeros_like(pos)
    for k in range(3):
        np.add.at(vn, tris[:, k], fn)
    key = np.round(pos * 20000).astype(np.int64)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.ravel()
    acc = np.zeros((inv.max() + 1, 3))
    np.add.at(acc, inv, vn)
    vn = acc[inv]
    return vn / (np.linalg.norm(vn, axis=1, keepdims=True) + 1e-12)


def clean_tris(tris, n):
    tris = np.asarray(tris, np.int64)
    return tris[(tris < n).all(1)]


def mesh_edges(tris):
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    return np.unique(np.sort(e, 1), axis=0)


def weld_map(pos):
    """Точки с одной позицией (швы UV) -> общий номер, чтобы сглаживание шло через швы."""
    key = np.round(np.asarray(pos) * 20000).astype(np.int64)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    return inv.ravel()


def smooth_on_mesh(vals, edges, n, iters, keep=0.5, weld=None):
    """Сглаживание значений по соседям сетки (Лаплас). vals: N или N x k."""
    v = np.asarray(vals, np.float64)
    one = v.ndim == 1
    if one:
        v = v[:, None]
    if weld is not None:
        # сглаживаем на сваренной сетке: швы не рвутся
        m = weld.max() + 1
        acc = np.zeros((m, v.shape[1])); cnt = np.zeros(m)
        np.add.at(acc, weld, v); np.add.at(cnt, weld, 1)
        r = smooth_on_mesh(acc / cnt[:, None], np.unique(np.sort(weld[edges], 1), axis=0), m, iters, keep)
        r = r[weld]
        return r[:, 0] if one else r
    e = edges[edges[:, 0] != edges[:, 1]]
    deg = np.zeros(n)
    np.add.at(deg, e[:, 0], 1); np.add.at(deg, e[:, 1], 1)
    has = deg > 0
    for _ in range(iters):
        s = np.zeros_like(v)
        np.add.at(s, e[:, 0], v[e[:, 1]]); np.add.at(s, e[:, 1], v[e[:, 0]])
        v = v.copy()
        v[has] = keep * v[has] + (1 - keep) * s[has] / deg[has][:, None]
    return v[:, 0] if one else v


def face_orientation(pos, tris, normals):
    """+1, если обход треугольников совпадает с нормалями вершин, иначе -1."""
    fn = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
    return 1.0 if (fn * normals[tris[:, 0]]).sum() >= 0 else -1.0


def normal_offsets(van, n_van, body, n_body, tris, reach=0.15):
    """Сдвиг каждой точки обычного тела вдоль её нормали до поверхности тела из мода.
    Луч в обе стороны; попадание засчитываем только в поверхность, смотрящую туда же
    (не в другую ногу и не во вторую грудь). Нет попадания - ближайшая точка поверхности."""
    tris = clean_tris(tris, len(body))
    tree = BVHTree.FromPolygons(body.tolist(), tris.tolist())
    o = face_orientation(body, tris, n_body)
    d = np.zeros(len(van))
    eps = 0.0005
    for i, (p, n) in enumerate(zip(van.tolist(), n_van.tolist())):
        p, n = Vector(p), Vector(n)
        best = None
        for sgn in (1.0, -1.0):
            dv = n * sgn
            loc, fn, _, dist = tree.ray_cast(p - dv * eps, dv, reach + eps)
            if loc is None or fn.dot(n) * o < 0.2:
                continue
            dist = (dist - eps) * sgn
            if best is None or abs(dist) < abs(best):
                best = dist
        if best is None:
            loc, fn, _, _ = tree.find_nearest(p, reach)
            best = (loc - p).dot(n) if loc is not None else 0.0
        d[i] = best
    return d


def pull_out(van, n_van, s, body, n_body, tris, iters=4):
    """Точки обычного тела, сдвинутые на s вдоль нормали, не должны оставаться внутри нового тела."""
    tris = clean_tris(tris, len(body))
    tree = BVHTree.FromPolygons(body.tolist(), tris.tolist())
    o = face_orientation(body, tris, n_body)
    s0, s = s, s.copy()
    for _ in range(iters):
        moved = van + n_van * s[:, None]
        changed = 0
        for i, p in enumerate(moved.tolist()):
            loc, fn, _, _ = tree.find_nearest(p, 0.03)
            if loc is None:
                continue
            fn = fn * o
            cosv = Vector(n_van[i]).dot(fn)
            if cosv < 0.3:  # ближайшая - чужая поверхность (другая нога, складка) - не трогаем
                continue
            g = (Vector(p) - loc).dot(fn)
            if g < -0.0002:
                # вдоль своей нормали нужно пройти больше, если поверхность наклонена; не больше 3 см всего
                s[i] = min(s[i] + min(-g / cosv, 0.01), s0[i] + 0.03)
                changed += 1
        if not changed:
            break
    return s


def vanilla_tris(z, part):
    if part + '_van_tris' in z:
        return z[part + '_van_tris']
    # старые профили: треугольники обычного тела берём из кэша игры
    import common as C
    from meshpos import MeshPos
    return MeshPos(os.path.join(C.CACHE, 'vanilla_json', part + '.mesh.json')).lod0()[1]


class Obstacle:
    """Поверхность, под которую нельзя уводить ткань: кожа или оболочка татуировки.
    normals - нормали вершин наружу (по ним определяется, где "снаружи")."""

    def __init__(self, pos, tris, normals, keep=None):
        """keep - какие вершины учитывать (остальные треугольники не препятствие)."""
        self.pos = np.asarray(pos, np.float64)
        self.tris = clean_tris(tris, len(self.pos))
        if keep is not None:
            self.tris = self.tris[np.asarray(keep)[self.tris].all(1)]
        self.tree = BVHTree.FromPolygons(self.pos.tolist(), self.tris.tolist())
        # "наружу" для каждого треугольника отдельно: у оболочек татуировок обход граней бывает разный
        nv = np.asarray(normals, np.float64)
        t = self.tris
        fn = np.cross(self.pos[t[:, 1]] - self.pos[t[:, 0]], self.pos[t[:, 2]] - self.pos[t[:, 0]])
        self.fs = np.where((fn * (nv[t[:, 0]] + nv[t[:, 1]] + nv[t[:, 2]])).sum(1) >= 0, 1.0, -1.0).tolist()

    def gap(self, points, reach=0.03):
        """Зазор до поверхности (минус - под ней) и нормаль наружу; inf - поверхность далеко."""
        n = len(points)
        gap = np.full(n, np.inf); nrm = np.zeros((n, 3))
        tree, fs = self.tree, self.fs
        for i, p in enumerate(np.asarray(points, np.float64).tolist()):
            l, f, fi, d = tree.find_nearest(p, reach)
            if l is not None:
                f = f * fs[fi]
                h = (Vector(p) - l).dot(f)
                # ближайшая точка сбоку (край: срез тела на шее, край оболочки татуировки) - это не поверхность под тканью
                if d > 0.002 and abs(h) < 0.7 * d:
                    continue
                gap[i] = h; nrm[i] = f
        return gap, nrm


SHELL_ON_SKIN = 0.005  # м: оболочка татуировки дальше от кожи - висящий кусок (у подмышек), не препятствие


def tattoo_obstacles(body, new_pos, new_tris, van_pos, van_tris):
    """[(оболочка на этом теле, она же на обычном теле)] как препятствия для ткани."""
    out = []
    for p, t, idx, nrm, gap in ((new_pos, new_tris, body.surf_index, body.n_surf, body.surf_gap),
                                (van_pos, van_tris, body.van_index, body.n_van, body.van_gap)):
        p = np.asarray(p, np.float64)
        h, _ = gap(p, 0.03)
        out.append(Obstacle(p, t, outward_normals(p, t, idx, nrm), keep=np.isfinite(h) & (h <= SHELL_ON_SKIN)))
    return [tuple(out)]


def outward_normals(pos, tris, ref_index, ref_normals):
    """Нормали оболочки, развёрнутые наружу по коже под ней (у татуировок в файлах игры они внутрь)."""
    n = vertex_normals(pos, clean_tris(tris, len(pos)))
    idx, _ = ref_index.query(pos, 1)
    n[(n * ref_normals[idx[:, 0]]).sum(1) < 0] *= -1
    return n


class Body:
    """Профиль тела, загруженный из папки профиля (profile.json + body.npz)."""

    def __init__(self, folder):
        self.folder = folder
        self.info = json.load(open(os.path.join(folder, 'profile.json'), encoding='utf-8'))
        z = np.load(os.path.join(folder, 'body.npz'))
        self.z = z
        VAN, DEL, NV, S, NS, VT = [], [], [], [], [], []
        self.display = []  # (имя части, позиции тела, треугольники)
        off = 0
        for part in PARTS:
            if part + '_van' not in z:
                continue
            van, n_van = z[part + '_van'], z[part + '_van_n']
            body, n_body, tris = z[part + '_body'], z[part + '_body_n'], z[part + '_body_tris']
            vt = clean_tris(vanilla_tris(z, part), len(van))
            if van.shape == body.shape:
                d = body - van
            elif BVHTree is not None:
                # Сетка тела отличается от игровой: сдвиг вдоль нормали до поверхности (лучом),
                # потом убираем выбросы и сглаживаем по сетке обычного тела
                s = normal_offsets(van, n_van, body, n_body, tris)
                e = mesh_edges(vt)
                wm = weld_map(van)
                med = smooth_on_mesh(s, e, len(van), 2, 0.0, wm)
                bad = np.abs(s - med) > 0.01
                s[bad] = med[bad]
                s = smooth_on_mesh(s, e, len(van), 2, 0.5, wm)
                # сглаживание срезает пики (грудь растёт на 5+ см): что осталось внутри нового тела,
                # дотягиваем до поверхности
                s = pull_out(van, n_van, s, body, n_body, tris)
                d = n_van * s[:, None]
            else:
                idx, _ = Index(body).query(van, 1)
                d = n_van * ((body[idx[:, 0]] - van) * n_van).sum(1)[:, None]
            VAN.append(van); DEL.append(d); NV.append(n_van); S.append(body); NS.append(n_body)
            VT.append(vt + off); off += len(van)
            self.display.append((part, body, tris))
        self.van, self.delta, self.n_van = map(np.concatenate, (VAN, DEL, NV))
        self.van_tris = np.concatenate(VT)
        self.surf, self.n_surf = np.concatenate(S), np.concatenate(NS)
        off, st = 0, []
        for _, p, t in self.display:
            st.append(clean_tris(t, len(p)) + off); off += len(p)
        self.surf_tris = np.concatenate(st)
        self.van_index = Index(self.van)
        self.surf_index = Index(self.surf)
        self._van_tree = None
        self._surf_obs = self._van_obs = None
        self._shell = {}

    def surf_gap(self, points, reach=0.03):
        """Зазор до поверхности нового тела (минус - под кожей) и нормаль наружу; inf - тело далеко."""
        if self._surf_obs is None:
            self._surf_obs = Obstacle(self.surf, self.surf_tris, self.n_surf)
        return self._surf_obs.gap(points, reach)

    def van_gap(self, points, reach=0.03):
        """То же для обычного тела игры (как вещь сидела у автора)."""
        if self._van_obs is None:
            self._van_obs = Obstacle(self.van, self.van_tris, self.n_van)
        return self._van_obs.gap(points, reach)

    def van_closest(self, points):
        """Ближайшая точка поверхности обычного тела: (треугольник N x 3 вершин, барицентр. веса, расстояние)."""
        if self._van_tree is None:
            self._van_tree = BVHTree.FromPolygons(self.van.tolist(), self.van_tris.tolist())
        tree = self._van_tree
        n = len(points)
        fi = np.zeros(n, np.int64); loc = np.zeros((n, 3)); dist = np.full(n, np.inf)
        for i, p in enumerate(np.asarray(points, np.float64).tolist()):
            l, _, f, d = tree.find_nearest(p)
            if l is not None:
                fi[i] = f; loc[i] = l; dist[i] = d
        tri = self.van_tris[fi]
        a, b, c = (self.van[tri[:, k]] for k in range(3))
        v0, v1, v2 = b - a, c - a, loc - a
        d00 = (v0 * v0).sum(1); d01 = (v0 * v1).sum(1); d11 = (v1 * v1).sum(1)
        d20 = (v2 * v0).sum(1); d21 = (v2 * v1).sum(1)
        den = d00 * d11 - d01 * d01
        den[np.abs(den) < 1e-20] = 1e-20
        v = (d11 * d20 - d01 * d21) / den
        w = (d00 * d21 - d01 * d20) / den
        bc = np.clip(np.stack([1 - v - w, v, w], 1), 0, 1)
        bc /= bc.sum(1, keepdims=True) + 1e-12
        return tri, bc, dist

    # ---------- татуировки ----------
    def tattoos(self):
        """[(ключ, есть ли версия под это тело)] для татуировок, которые есть в профиле."""
        return [(t, bool(v)) for t, v in sorted(self.info.get('tattoos', {}).items())]

    def tattoo_mesh(self, key):
        """Оболочка татуировки, как она лежит на этом теле: позиции, нормали, треугольники."""
        z = self.z
        if key + '_body' in z:
            return z[key + '_body'], z[key + '_body_n'], z[key + '_body_tris']
        return z[key + '_van'], z[key + '_van_n'], z[key + '_van_tris']

    def field(self, shell=None):
        """Сдвиги точек обычного тела. shell - татуировка без версии под тело: под её оболочкой
        тело не "худеет" внутрь, иначе одежда уйдёт под старую оболочку и рисунок проступит."""
        if not shell:
            return self.delta
        if shell not in self._shell:
            sh = self.z[shell + '_van']
            _, ds = Index(sh).query(self.van, 1)
            w = np.clip(1 - (np.sqrt(ds[:, 0]) - SHELL_RADIUS) / SHELL_FADE, 0, 1)
            inward = np.minimum((self.delta * self.n_van).sum(1), 0)
            self._shell[shell] = self.delta - self.n_van * (inward * w)[:, None]
        return self._shell[shell]


CLOTH_SMOOTH = 4      # проходов сглаживания сдвигов по сетке ткани (убирает мятость и "кашу" между ногами)


def autofit(points, body, shell=None, tris=None):
    """Подогнанные позиции для точек одежды (N x 3, координаты игры).
    tris - треугольники одежды: с ними сдвиги ещё и сглаживаются по ткани."""
    points = np.asarray(points, np.float64)
    delta = body.field(shell)
    if BVHTree is not None:
        # Сдвиг в ближайшей точке поверхности тела, плавно по треугольнику (без "ямок" у вершин)
        tri, bc, d0 = body.van_closest(points)
        near = (delta[tri] * bc[..., None]).sum(1)
        nn = (body.n_van[tri] * bc[..., None]).sum(1)
    else:
        idx, dd = body.van_index.query(points, K)
        w = 1.0 / (dd ** 2 + 1e-14)
        w /= w.sum(1, keepdims=True)
        near = (delta[idx] * w[..., None]).sum(1)
        nn = (body.n_van[idx] * w[..., None]).sum(1)
        d0 = np.sqrt(dd[:, 0])
    # Небольшой отступ от кожи вдоль нормали тела: плавно затухает с 1 до 3 см от тела
    nn /= np.linalg.norm(nn, axis=1, keepdims=True) + 1e-12
    near = near + nn * (INFLATE * np.clip(1 - (d0 - 0.01) / 0.02, 0, 1))[:, None]
    t = np.clip((d0 - NEAR_D) / (FAR_D - NEAR_D), 0, 1)
    out = points + near * (1 - t)[:, None]
    # Свободные части: гладкое среднее большой области, чтобы низ юбки не "водило"
    # между ногами из-за того, что ближайшей оказывается то одна, то другая нога.
    loose = t > 0
    if loose.any():
        fi, fd = body.van_index.query(points[loose], K_FAR)
        wf = 1.0 / (np.sqrt(fd) + 0.01)
        far = (delta[fi] * (wf / wf.sum(1, keepdims=True))[..., None]).sum(1)
        out[loose] += far * t[loose][:, None]
    if tris is not None and CLOTH_SMOOTH:
        # Сглаживаем не форму, а сдвиг: складки и швы автора остаются, а соседние точки ткани
        # двигаются согласованно (иначе в паху точки "прыгают" между ногами и ткань мнётся)
        tr = clean_tris(tris, len(points))
        disp = smooth_on_mesh(out - points, mesh_edges(tr), len(points), CLOTH_SMOOTH, 0.5, weld_map(points))
        out = points + disp
    return out


def plane_gap(points, index, surf, normals, k=4):
    """Гладкий зазор до поверхности: точку сравниваем с усреднённой плоскостью соседних точек тела.
    Возвращает зазор, нормаль этой плоскости и расстояние до ближайшей точки тела."""
    idx, dd = index.query(points, k)
    w = 1.0 / (dd ** 2 + 1e-14)
    w /= w.sum(1, keepdims=True)
    s = (surf[idx] * w[..., None]).sum(1)
    n = (normals[idx] * w[..., None]).sum(1)
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    return ((points - s) * n).sum(1), n, np.sqrt(dd[:, 0])


def conform(current, original, body, near=0.03, far=0.06):
    """Прилегание на новом месте: каждая точка ткани у тела получает тот же зазор до нового тела,
    какой у неё был у автора до обычного тела. Опустил юбку на бёдра - она обхватит бёдра,
    а не останется шириной талии. Свободные части (дальше near..far) не трогаем."""
    g0, _, _ = plane_gap(original, body.van_index, body.van, body.n_van)
    g1, n1, d1 = plane_gap(current, body.surf_index, body.surf, body.n_surf)
    want = np.maximum(g0, INFLATE)
    t = np.clip(1 - (d1 - near) / (far - near), 0, 1)
    # Только наружу: там, где ткань оказалась ближе к телу, чем у автора (или внутри), отодвигаем.
    push = np.maximum(want - g1, 0) * t
    act = t > 0
    if not act.any():
        return current.copy()
    # Сглаживаем поле сдвигов по соседним точкам ткани, иначе получаются "шипы"
    disp = n1 * push[:, None]
    sub = np.nonzero(act)[0]
    idx, d2 = Index(current[sub]).query(current[sub], 16)
    sw = np.exp(-d2 / (2 * 0.006 ** 2))
    sw /= sw.sum(1, keepdims=True)
    smooth = np.zeros_like(current)
    smooth[sub] = (disp[sub][idx] * sw[..., None]).sum(1)
    # сглаживание "размазывает" пик, поэтому берём не меньше исходного сдвига вдоль нормали
    s_along = (smooth * n1).sum(1)
    along = np.maximum(s_along, push)
    return current + smooth + n1 * (along - s_along)[:, None]


SETTLE_ITERS = 12     # проходов натяжения ткани
MIN_GAP = 0.001       # м: ближе к коже ткань не подпускаем
FEATHER = 0.002       # м: насколько подъём ткани может "расплыться" на соседние точки


def settle(current, original, body, tris, iters=SETTLE_ITERS, shells=()):
    """Натяжение ткани после подгонки. Ткань стремится сохранить форму автора (свои складки и
    плавность), но не может уйти под кожу. Так под грудью ткань натягивается мостиком, а не
    прилипает к телу, и мелкие выступы тела (соски, косточки) не протыкают одежду.
    shells - [(оболочка татуировки на этом теле, она же на обычном теле)]: под неё ткань тоже
    не уходит, иначе в игре рисунок проступит сквозь одежду."""
    if BVHTree is None or tris is None:
        return current
    n = len(current)
    wm = weld_map(original)
    m = wm.max() + 1
    cnt = np.zeros(m); np.add.at(cnt, wm, 1)

    def weld(a):
        acc = np.zeros((m, 3)); np.add.at(acc, wm, a)
        return acc / cnt[:, None]

    X, X0 = weld(current), weld(original)
    E = np.unique(np.sort(wm[mesh_edges(clean_tris(tris, n))], 1), axis=0)
    E = E[E[:, 0] != E[:, 1]]
    deg = np.zeros(m); np.add.at(deg, E[:, 0], 1); np.add.at(deg, E[:, 1], 1)
    has = deg > 0

    def avg(Y):
        s = np.zeros_like(Y)
        np.add.at(s, E[:, 0], Y[E[:, 1]]); np.add.at(s, E[:, 1], Y[E[:, 0]])
        out = Y.copy()
        out[has] = s[has] / deg[has][:, None]
        return out

    L0 = avg(X0) - X0
    g, _ = body.surf_gap(X, 0.04)
    act = np.isfinite(g).astype(np.float64)[:, None]  # свободные части (далеко от тела) не трогаем

    # препятствия: (зазор на новом теле, зазор у автора на обычном теле, дальность)
    obstacles = [(body.surf_gap, body.van_gap, 0.03)]
    obstacles += [(new.gap, van.gap, 0.015) for new, van in shells]

    def allowed(P0, k):
        """Допустимый зазор: MIN_GAP, а там, где у автора ткань и так заходила под кожу или под
        оболочку татуировки (подгибы рукавов, швы), - как было у автора. Это задумано, не ошибка."""
        g0, _ = obstacles[k][1](P0, obstacles[k][2])
        return np.minimum(MIN_GAP, np.where(np.isfinite(g0), g0, MIN_GAP))

    allow_v = [allowed(X0, k) for k in range(len(obstacles))]

    SKIN, SHELLS = [0], list(range(1, len(obstacles)))

    def collide(Y, ks=None):
        for k in (range(len(obstacles)) if ks is None else ks):
            gap_new, _, reach = obstacles[k]
            g, nrm = gap_new(Y, reach)
            al = allow_v[k]
            bad = g < al
            Y[bad] += nrm[bad] * np.minimum(al[bad] - g[bad], 0.03)[:, None]
        return Y

    tw = clean_tris(wm[clean_tris(tris, n)], m)
    tw = tw[(tw[:, 0] != tw[:, 1]) & (tw[:, 1] != tw[:, 2]) & (tw[:, 0] != tw[:, 2])]
    samples = np.array([(1 / 3, 1 / 3, 1 / 3), (0.5, 0.5, 0), (0, 0.5, 0.5), (0.5, 0, 0.5)])
    allow_f = [[allowed((X0[tw] * bc[None, :, None]).sum(1), k) for bc in samples] for k in range(len(obstacles))]

    def collide_faces(Y, ks=None):
        """Выступы тела меньше треугольника ткани (соски, косточки): точки ткани снаружи, а грань
        между ними режет кожу. Проверяем центр и середины сторон и поднимаем весь треугольник."""
        lift = np.zeros(m); dirs = np.zeros((m, 3))
        for k in (range(len(obstacles)) if ks is None else ks):
            gap_new, _, reach = obstacles[k]
            for bc, al in zip(samples, allow_f[k]):
                P = (Y[tw] * bc[None, :, None]).sum(1)
                g, nrm = gap_new(P, min(reach, 0.02))
                bad = np.nonzero(g < al)[0]
                if not len(bad):
                    continue
                need = np.minimum((al[bad] - g[bad]) / (bc * bc).sum(), 0.01)  # не больше 1 см за проход
                for c in range(3):
                    if bc[c] == 0:
                        continue
                    v = tw[bad, c]
                    np.maximum.at(lift, v, need * bc[c])
                    np.add.at(dirs, v, nrm[bad] * (need * bc[c])[:, None])
        hit = lift > 0
        dn = dirs[hit] / (np.linalg.norm(dirs[hit], axis=1, keepdims=True) + 1e-12)
        Y[hit] += dn * lift[hit][:, None]
        return Y, int(hit.sum())

    def dilate(v):
        r = v.copy()
        np.maximum.at(r, E[:, 0], v[E[:, 1]]); np.maximum.at(r, E[:, 1], v[E[:, 0]])
        return r

    def feathered(step, Y):
        """Выталкивание с плавными краями: подъём ткани расплывается на соседние точки (не меньше
        нужного), чтобы ткань не повторяла бугристую оболочку татуировки (она на 1-6 мм над кожей)."""
        Yb = Y.copy()
        Y = step(Y)
        if isinstance(Y, tuple):
            Y = Y[0]
        D = Y - Yb
        mag = np.linalg.norm(D, axis=1)
        if not (mag > 1e-7).any():
            return Y
        m2 = dilate(dilate(mag))
        for _ in range(3):
            m2 = 0.5 * m2 + 0.5 * avg(m2[:, None])[:, 0]
        # расплываются только мелкие подъёмы (неровности): не больше FEATHER сверх нужного точке,
        # иначе большой подъём (подгиб рукава) раскрывает край воронкой
        m2 = np.clip(m2, mag, mag + FEATHER)
        Dd = D.copy()
        for _ in range(4):
            Dd = 0.5 * Dd + 0.5 * avg(Dd)
        Dd[mag > 1e-7] = D[mag > 1e-7]  # своё направление, если точку и так толкали
        nd = np.linalg.norm(Dd, axis=1)
        ok = nd > 1e-9
        out = Yb.copy()
        out[ok] += Dd[ok] / nd[ok][:, None] * m2[ok][:, None]
        return out

    def push(Y):
        """Кожа - как есть, оболочки татуировок - с плавными краями."""
        Y = collide(Y, SKIN)
        return feathered(lambda Z: collide(Z, SHELLS), Y) if SHELLS else Y

    X = push(X)
    for it in range(iters):
        X = X + 0.5 * ((avg(X) - L0) - X) * act
        if it % 3 == 2 or it == iters - 1:
            X = push(X)
    for _ in range(3):
        X, k = collide_faces(X, SKIN)
        if SHELLS:
            X = feathered(lambda Z: collide_faces(Z, SHELLS), X)
        elif not k:
            break
    X = collide(X)  # точно не под кожей и не под татуировкой
    out = X[wm]
    # точки, которые у автора не совпадали (разные позиции), получают свой исходный разнос обратно
    return out + (original - X0[wm])


def transfer(src_before, src_after, dst, k=4):
    """Переносит правки с одного набора точек на другой (например, с LOD0 на LOD1-2)."""
    disp = src_after - src_before
    idx, dd = Index(src_before).query(dst, k)
    w = 1.0 / (dd + 1e-10)
    w /= w.sum(1, keepdims=True)
    return dst + (disp[idx] * w[..., None]).sum(1)


def update_normals(m, ci, old, new):
    """Поворачивает нормали/касательные автора на столько, на сколько изменилась форма.
    Так сохраняется авторское сглаживание, а свет ложится по новой форме."""
    n, nw = m.read_dec4(ci, 'PS_Normal')
    if n is None:
        return
    tris = m.triangles(ci)
    change = vertex_normals(new, tris) - vertex_normals(old, tris)
    n2 = n + change
    n2 /= np.linalg.norm(n2, axis=1, keepdims=True) + 1e-12
    m.write_dec4(ci, 'PS_Normal', n2, nw)
    t, tw = m.read_dec4(ci, 'PS_Tangent')
    if t is not None:
        t2 = t - n2 * (t * n2).sum(1)[:, None]
        t2 /= np.linalg.norm(t2, axis=1, keepdims=True) + 1e-12
        m.write_dec4(ci, 'PS_Tangent', t2, tw)


def apply_positions(m, results):
    """Записывает новые позиции во все чанки и обновляет нормали."""
    olds = [m.read(ci) for ci in range(len(m.chunks))]
    m.set_quant(np.concatenate(results))
    for ci, p in enumerate(results):
        m.write(ci, p)
        update_normals(m, ci, olds[ci], p)
