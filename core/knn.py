# Поиск ближайших точек. В Blender - KD-дерево (mathutils, на C), в 50-100 раз быстрее перебора.
import numpy as np

try:
    from mathutils.kdtree import KDTree
except ImportError:  # вне Blender (тесты) - перебор блоками
    KDTree = None


class Index:
    """Индекс по набору точек: build один раз, потом много запросов."""

    def __init__(self, ref):
        self.ref = np.asarray(ref, np.float64)
        self.tree = None
        if KDTree is not None:
            t = KDTree(len(self.ref))
            for i, p in enumerate(self.ref.tolist()):
                t.insert(p, i)
            t.balance()
            self.tree = t

    def query(self, pts, k):
        """(индексы N x k, квадраты расстояний N x k), отсортировано по расстоянию."""
        pts = np.asarray(pts, np.float64)
        k = min(k, len(self.ref))
        if self.tree is not None:
            idx = np.empty((len(pts), k), np.int64)
            d = np.empty((len(pts), k))
            fn = self.tree.find_n
            for i, p in enumerate(pts.tolist()):
                r = fn(p, k)
                idx[i] = [x[1] for x in r]
                d[i] = [x[2] for x in r]
            return idx, d * d
        idx_all, d_all = [], []
        for a in range(0, len(pts), 1500):
            blk = pts[a:a + 1500]
            d2 = ((blk[:, None, :] - self.ref[None, :, :]) ** 2).sum(-1)
            idx = np.argpartition(d2, k - 1, axis=1)[:, :k] if k < len(self.ref) else np.tile(
                np.arange(len(self.ref)), (len(blk), 1))
            dd = np.take_along_axis(d2, idx, 1)
            o = np.argsort(dd, 1)
            idx_all.append(np.take_along_axis(idx, o, 1))
            d_all.append(np.take_along_axis(dd, o, 1))
        return np.concatenate(idx_all), np.concatenate(d_all)


_cache = {}


def index_for(key, ref):
    """Кэш индексов по ключу (тело не меняется за сеанс)."""
    if key not in _cache:
        _cache[key] = Index(ref)
    return _cache[key]


def nearest(pts, ref, k):
    return Index(ref).query(pts, k)
