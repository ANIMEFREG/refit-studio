# Чтение и запись позиций вершин прямо в JSON меша WolvenKit (без glb).
# Так сохраняются все остальные данные меша, включая Garment Support (PS_ExtraData).
# Буфер читается через numpy-представления с шагом (stride), без цикла по точкам.
import json, base64
import numpy as np

SIZES = {'PT_Short4N': 8, 'PT_UByte4': 4, 'PT_UByte4N': 4, 'PT_Float16_2': 4, 'PT_Float16_4': 8,
         'PT_Dec4': 4, 'PT_Color': 4, 'PT_Float2': 8, 'PT_Float3': 12, 'PT_Float4': 16,
         'PT_UShort4': 8, 'PT_Short4': 8, 'PT_UShort2': 4, 'PT_UInt1': 4, 'PT_Float1': 4,
         'PT_Short2N': 4, 'PT_UShort4N': 8, 'PT_Byte4N': 4}


def els(x):
    return x['Elements'] if isinstance(x, dict) and 'Elements' in x else x


def has_geometry(path):
    """False для файлов-хранилищ материалов (меш без 3D-модели)."""
    j = json.load(open(path, encoding='utf-8-sig'))
    return bool((j['Data']['RootChunk'].get('renderResourceBlob') or {}).get('Data'))


class MeshPos:
    def __init__(self, path):
        self.path = path
        self.j = json.load(open(path, encoding='utf-8-sig'))
        blob = self.j['Data']['RootChunk']['renderResourceBlob']['Data']
        self.h = blob['header']
        self.rb = blob['renderBuffer']
        self.buf = bytearray(base64.b64decode(self.rb['Bytes']))
        qs, qo = self.h['quantizationScale'], self.h['quantizationOffset']
        self.scale = np.array([qs['X'], qs['Y'], qs['Z']], np.float64)
        self.off = np.array([qo['X'], qo['Y'], qo['Z']], np.float64)
        self.chunk_info = els(self.h['renderChunkInfos'])
        self.chunks = []
        for ch in self.chunk_info:
            cv = ch['chunkVertices']
            stream0 = [e for e in els(cv['vertexLayout']['elements'])
                       if e['streamIndex'] == 0 and e['usage'] != 'PS_Invalid']
            stride, posoff = 0, None
            for e in stream0:
                if e['usage'] == 'PS_Position':
                    posoff = stride
                stride += SIZES[e['type']]
            self.chunks.append((ch['numVertices'], els(cv['byteOffsets'])[0], stride, posoff))

    def _view(self, offset, n, stride, dtype, width):
        return np.ndarray((n, width), dtype=dtype, buffer=self.buf, offset=offset,
                          strides=(stride, np.dtype(dtype).itemsize))

    def lod_mask(self, ci):
        return self.chunk_info[ci].get('lodMask', 1)

    def triangles(self, ci):
        """Треугольники чанка (индексы ushort/uint из общего индексного буфера)."""
        ch = self.chunk_info[ci]
        ib = ch['chunkIndices']
        off = self.h['indexBufferOffset'] + ib['teOffset']
        count = ch['numIndices']
        dt = '<u4' if ib.get('pe', 'IBCT_IndexUShort') == 'IBCT_IndexUInt' else '<u2'
        idx = np.frombuffer(bytes(self.buf), dtype=dt, count=count, offset=off)
        return idx.reshape(-1, 3).astype(np.int64)

    def _pos_view(self, ci):
        n, base, stride, posoff = self.chunks[ci]
        return self._view(base + posoff, n, stride, '<i2', 4)

    def read(self, ci):
        q = self._pos_view(ci)[:, :3].astype(np.float64)
        return q / 32767.0 * self.scale + self.off

    def write(self, ci, pos):
        q = np.clip(np.round((np.asarray(pos) - self.off) / self.scale * 32767.0), -32767, 32767)
        self._pos_view(ci)[:, :3] = q.astype(np.int16)

    def read_all(self):
        return np.concatenate([self.read(i) for i in range(len(self.chunks))])

    def set_quant(self, all_pos):
        """Расширяет диапазон квантования, если новые позиции вышли за старый."""
        lo, hi = all_pos.min(0), all_pos.max(0)
        need = np.maximum(np.abs(lo - self.off), np.abs(hi - self.off)) * 1.001
        if np.all(need <= self.scale):
            return False
        self.scale = np.maximum(self.scale, need)
        qs = self.h['quantizationScale']
        qs['X'], qs['Y'], qs['Z'] = [float(v) for v in self.scale]
        return True

    def _stream_offset(self, ci, usage):
        """Смещение и шаг элемента usage (PS_Normal/PS_Tangent) в его потоке."""
        cv = self.chunk_info[ci]['chunkVertices']
        elems = [e for e in els(cv['vertexLayout']['elements']) if e['usage'] != 'PS_Invalid'
                 and e.get('streamType', 'ST_PerVertex') == 'ST_PerVertex']
        target = [e for e in elems if e['usage'] == usage and e.get('usageIndex', 0) == 0]
        if not target or target[0]['type'] != 'PT_Dec4':
            return None
        si = target[0]['streamIndex']
        stride, off = 0, None
        for e in elems:
            if e['streamIndex'] != si:
                continue
            if e is target[0]:
                off = stride
            stride += SIZES[e['type']]
        return els(cv['byteOffsets'])[si], stride, off

    def read_dec4(self, ci, usage):
        """Нормали/касательные PT_Dec4: 10 бит на ось (0..1023 -> -1..1) + 2 бита w."""
        so = self._stream_offset(ci, usage)
        if so is None:
            return None, None
        base, stride, off = so
        raw = self._view(base + off, self.chunks[ci][0], stride, '<u4', 1)[:, 0].copy()
        c = np.stack([raw & 1023, (raw >> 10) & 1023, (raw >> 20) & 1023], 1).astype(np.float64)
        return c / 1023.0 * 2 - 1, raw >> 30

    def write_dec4(self, ci, usage, vec, wbits):
        base, stride, off = self._stream_offset(ci, usage)
        q = np.clip(np.round((vec + 1) / 2 * 1023), 0, 1023).astype(np.uint32)
        raw = q[:, 0] | (q[:, 1] << 10) | (q[:, 2] << 20) | (wbits.astype(np.uint32) << 30)
        self._view(base + off, self.chunks[ci][0], stride, '<u4', 1)[:, 0] = raw

    def normals_all(self):
        """Нормали всех чанков (в осях меша), для тех, где их нет, - None."""
        out = []
        for ci in range(len(self.chunks)):
            n, _ = self.read_dec4(ci, 'PS_Normal')
            out.append(n)
        return out

    def lod0(self):
        """Чанки первого (самого подробного) LOD: позиции, треугольники и границы чанков."""
        pos, tris, ranges, a = [], [], [], 0
        for ci in range(len(self.chunks)):
            if not (self.lod_mask(ci) & 1):
                continue
            p = self.read(ci)
            pos.append(p)
            tris.append(self.triangles(ci) + a)
            ranges.append((ci, a, a + len(p)))
            a += len(p)
        return np.concatenate(pos), np.concatenate(tris), ranges

    def save(self, path):
        self.rb['Bytes'] = base64.b64encode(bytes(self.buf)).decode('ascii')
        with open(path, 'w', encoding='utf-8') as f:  # закрываем сразу: следом файл читает WolvenKit
            json.dump(self.j, f, indent=1)
