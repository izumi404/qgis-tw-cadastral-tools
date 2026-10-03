"""QGIS 3.44：高雄範圍內登記建物完成日期。

外掛的範圍查詢引擎；由 Processing 工具呼叫。
快取資料夾支援續跑；需要更新資料時請改用新的空資料夾。
點位是高雄 GIS 的 x97/y97 定位點，不是建物輪廓；重疊單位保留。
來源只涵蓋可查詢的登記建號，未經官方全量核證。
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import time
from collections import deque
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, parse_qs, urlsplit
from urllib.request import Request, urlopen

from qgis.PyQt.QtCore import QVariant, QDate
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY,
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingContext,
    QgsProcessingException, QgsProcessingFeedback, QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink, QgsProcessingParameterFile,
    QgsVectorLayer, QgsWkbTypes,
)

API = 'https://gisdawh.kcg.gov.tw/embed/KCBlockAPI/API.cfm'
MOI = 'https://easymap.moi.gov.tw/Z10Web/City_json_getSectionList'
CAP = 1000
CRS = 'EPSG:3826'


def date_value(raw):
    raw = str(raw or '').strip()
    if raw in ('', '0000000'):
        return None, '缺漏'
    if re.fullmatch(r'\d{7}', raw):
        try:
            d = dt.date(int(raw[:3]) + 1911, int(raw[3:5]), int(raw[5:]))
            return QDate(d.year, d.month, d.day), '有效'
        except ValueError:
            pass
    return None, '格式或日期無效'


class Source:
    def __init__(self, folder, feedback):
        folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(folder / 'building_location_cache.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS queries (key TEXT PRIMARY KEY, payload TEXT)')
        self.feedback, self.last, self.requests = feedback, 0.0, 0

    def check(self):
        if self.feedback.isCanceled():
            raise QgsProcessingException('已取消；快取已保留，可用相同資料夾續跑。')

    def get(self, params, url=API):
        self.check()
        key = url + '?' + urlencode(sorted(params.items()))
        saved = self.db.execute('SELECT payload FROM queries WHERE key=?', (key,)).fetchone()
        if saved:
            return json.loads(saved[0])
        for attempt in range(3):
            self.check()
            time.sleep(max(0, 1.0 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            self.requests += 1
            try:
                request = Request(key, headers={'User-Agent': 'QGIS-BuildingDates/1.0', 'Accept': 'application/json'})
                with urlopen(request, timeout=60) as response:
                    data = json.loads(response.read().decode('utf-8-sig'))
                if isinstance(data, dict) and data.get('isError') not in (False, 0):
                    raise QgsProcessingException('來源回報錯誤；已停止，沒有把錯誤當成零筆。')
                if not isinstance(data, (dict, list)):
                    raise QgsProcessingException('來源格式異常。')
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO queries VALUES (?,?)',
                                    (key, json.dumps(data, ensure_ascii=False)))
                return data
            except HTTPError as exc:
                if exc.code in (401, 403, 429) or exc.code < 500 or attempt == 2:
                    raise QgsProcessingException(f'HTTP {exc.code}，停止查詢；快取已保留。') from exc
            except (URLError, TimeoutError, json.JSONDecodeError):
                if attempt == 2:
                    raise
            time.sleep(2 ** (attempt + 1))

    def listing(self, item, **kwargs):
        result = self.get({'SERVICE': 'GETLIST', 'ITEM': item,
                           'hasGeometry': 'true' if item != 'build' else 'false', 'SRS': '', **kwargs})
        if not isinstance(result, dict) or not isinstance(result.get('list'), list):
            raise QgsProcessingException(f'{item}: 缺少 list，來源格式可能改變。')
        return result['list']

    def buildings(self, section, prefix=''):
        # 廣度優先先取得短搜尋字串；未截斷的包含搜尋可完整覆蓋更長前綴。
        # 例如「2」結果少於1000筆，就不需再次查「02」「002」等前綴。
        complete = []
        for key, payload in self.db.execute('SELECT key,payload FROM queries WHERE key LIKE ?',
                                             (API + '?DD48=' + section + '&%',)):
            params = parse_qs(urlsplit(key).query, keep_blank_values=True)
            cached = json.loads(payload)
            if params.get('ITEM') == ['build'] and isinstance(cached.get('list'), list) and len(cached['list']) < CAP:
                complete.append((params['DD49'][0], cached['list']))
        queue = deque([prefix])
        while queue:
            self.check()
            current = queue.popleft()
            covered = next(((term, rows) for term, rows in complete if term in current), None)
            if covered is not None:
                query_term, rows = covered
                is_complete = True
            else:
                query_term = current
                rows = self.listing('build', DD48=section, DD49=current)
                is_complete = len(rows) < CAP
                if is_complete:
                    complete.append((current, rows))
                self.feedback.pushInfo(f'{section}/{current or "全部"}: {len(rows)} 筆（含字串搜尋）')
            for row in rows:
                number = str(row.get('dd49', ''))
                if row.get('dd48') != section or not re.fullmatch(r'\d{8}', number) or query_term not in number:
                    raise QgsProcessingException('建號或查詢語義異常，停止以免誤報完整。')
                if 'dd16' not in row or 'x97' not in row or 'y97' not in row:
                    raise QgsProcessingException('來源缺少日期或座標欄位。')
                if number.startswith(current):
                    yield row
            if not is_complete:
                if len(current) == 8:
                    raise QgsProcessingException('完整建號仍回傳滿額，無法核實資料完整性。')
                queue.extend(current + digit for digit in '0123456789')

    def lands(self, section):
        """Use uncapped branches and preserve multiple source pieces of one parcel."""
        complete, queue = [], deque([''])
        while queue:
            self.check()
            prefix = queue.popleft()
            covered = next(((term, rows) for term, rows in complete if term in prefix), None)
            if covered is None:
                term = prefix
                rows = self.listing('land', AA48=section, AA49=prefix)
                if len(rows) < CAP:
                    complete.append((term, rows))
            else:
                term, rows = covered
            for row in rows:
                number = str(row.get('aa49', ''))
                if row.get('aa48') != section or not re.fullmatch(r'\d{8}', number) or term not in number:
                    raise QgsProcessingException('地號或包含搜尋語義異常；停止以免誤報完整。')
            if len(rows) >= CAP:
                if len(prefix) == 8:
                    raise QgsProcessingException('完整地號仍達查詢上限，無法確認清單完整。')
                queue.extend(prefix + digit for digit in '0123456789')
                # A capped response can stop halfway through a multi-piece parcel.
                continue
            grouped = {}
            for row in rows:
                if row['aa49'].startswith(prefix):
                    grouped.setdefault(row['aa49'], []).append(row)
            for number, pieces in grouped.items():
                self.check()
                row = dict(pieces[0])
                attrs = {k: v for k, v in row.items() if k not in ('wkt', 'bbox')}
                if any({k: v for k, v in p.items() if k not in ('wkt', 'bbox')} != attrs for p in pieces):
                    raise QgsProcessingException(f'{section}/{number} 同一地號屬性衝突；請用新快取核對。')
                wkts = list(dict.fromkeys(p.get('wkt') or '' for p in pieces))
                if len(wkts) > 1:
                    geometries = [QgsGeometry.fromWkt(w) for w in wkts]
                    if any(g.isEmpty() or not g.isGeosValid() for g in geometries):
                        raise QgsProcessingException(f'{section}/{number} 多片宗地圖形無效，不能安全合併。')
                    merged = QgsGeometry.unaryUnion(geometries)
                    if merged.isEmpty() or merged.lastError() or not merged.isGeosValid():
                        raise QgsProcessingException(f'{section}/{number} 多片宗地合併失敗。')
                    row['wkt'] = merged.asWkt()
                    box = merged.boundingBox()
                    row['bbox'] = [box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()]
                row['source_parts'] = len(wkts)
                yield row


def valid_polygon(wkt):
    if not wkt:
        return None
    geometry = QgsGeometry.fromWkt(wkt)
    if geometry.isNull() or geometry.isEmpty():
        return None
    if not geometry.isGeosValid():
        geometry = geometry.makeValid()
    return geometry if not geometry.isEmpty() else None


def candidates(source, area, feedback):
    # 粗略地段邊界只用來縮窄下載；最後以原始範圍精確篩點。
    search_area = area.buffer(100, 8)
    districts = source.listing('dist')
    selected = []
    sections = {}
    missing_boundaries = []
    for dist in districts:
        if not dist.get('code'):
            continue
        shape = valid_polygon(dist.get('wkt'))
        if shape is None:
            raise QgsProcessingException(f'行政區 {dist.get("name")} 缺少邊界，無法自動判斷查詢範圍。')
        if not shape.intersects(search_area):
            continue
        selected.append({'code': dist['code'], 'name': dist['name']})
        official = source.get({'cityCode': 'E', 'townCode': dist['code']}, url=MOI)
        if not isinstance(official, list) or not official:
            raise QgsProcessingException('內政部地段清單取得失敗。')
        geometric = {r['code']: r for r in source.listing('lnsect', DIST=dist['code']) if r.get('code')}
        combined = {r['id']: {'code': r['id'], 'name': r['name'], 'office': r['officeCode']} for r in official}
        for code, row in geometric.items():
            combined.setdefault(code, {'code': code, 'name': row['name'], 'office': ''})
        for code, row in combined.items():
            boundary = valid_polygon(geometric.get(code, {}).get('wkt'))
            if boundary is None or boundary.intersects(search_area):
                sections[code] = {**row, 'district': dist['name']}
                if boundary is None:
                    missing_boundaries.append(code)
    feedback.pushInfo('相交行政區：' + '、'.join(d['name'] for d in selected))
    feedback.pushInfo(f'候選地段 {len(sections)} 個；缺界線地段亦納入查詢。')
    return sorted(sections.values(), key=lambda r: r['code']), selected, missing_boundaries


class BuildingDatesAlgorithm(QgsProcessingAlgorithm):
    def name(self): return 'kaohsiung_building_dates_in_range'
    def displayName(self): return '高雄：範圍內建物完成日期（定位點）'
    def group(self): return '建物資料'
    def groupId(self): return 'building_dates'
    def createInstance(self): return BuildingDatesAlgorithm()
    def shortHelpString(self):
        return ('輸入多邊形，例如「計畫範圍」。自動查詢相交地段，以公開 x97/y97 建立 EPSG:3826 點位並篩入範圍。'
                '輸出建號、完成日期及地址；不是建物輪廓。同棟多個單位可重疊。建議儲存為 GeoPackage。'
                '快取可續跑，新一輪資料更新請用新資料夾。網站每次1000筆時遞迴拆查，可能需較長時間。'
                '缺少座標的建號另存快取資料夾內的 unlocated.json，不能判斷是否在界內。'
                'report.json 記錄查詢範圍與完整性；沒有官方全量保證。')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource('RANGE', '範圍多邊形', [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterFile('CACHE', '快取資料夾（續跑請選同一個）', behavior=QgsProcessingParameterFile.Folder))
        self.addParameter(QgsProcessingParameterFeatureSink('OUTPUT', '範圍內建物日期定位點', QgsProcessing.TypeVectorPoint))

    def processAlgorithm(self, parameters, context, feedback):
        boundary = self.parameterAsSource(parameters, 'RANGE', context)
        if boundary is None or not boundary.sourceCrs().isValid():
            raise QgsProcessingException('範圍必須有有效座標系統。')
        transform = QgsCoordinateTransform(boundary.sourceCrs(), QgsCoordinateReferenceSystem(CRS), context.transformContext())
        polygons = []
        for feature in boundary.getFeatures():
            geometry = QgsGeometry(feature.geometry())
            if geometry.isNull() or geometry.isEmpty():
                continue
            geometry.transform(transform)
            if not geometry.isGeosValid():
                geometry = geometry.makeValid()
            polygons.append(geometry)
        area = QgsGeometry.unaryUnion(polygons)
        if area.isNull() or area.isEmpty() or not area.isGeosValid():
            raise QgsProcessingException('範圍幾何無效或為空。')
        engine = QgsGeometry.createGeometryEngine(area.constGet())
        engine.prepareGeometry()
        fields = QgsFields()
        for name in ['district', 'section', 'sect_name', 'build_no', 'date_roc']:
            fields.append(QgsField(name, QVariant.String))
        fields.append(QgsField('finish_date', QVariant.Date))
        for name in ['date_state', 'address', 'floors', 'structure']:
            fields.append(QgsField(name, QVariant.String))
        fields.append(QgsField('x97', QVariant.Double))
        fields.append(QgsField('y97', QVariant.Double))
        fields.append(QgsField('source_url', QVariant.String))
        fields.append(QgsField('office', QVariant.String))
        fields.append(QgsField('area_m2', QVariant.Double))
        sink, destination = self.parameterAsSink(parameters, 'OUTPUT', context, fields,
                                                QgsWkbTypes.Point, QgsCoordinateReferenceSystem(CRS))
        if sink is None:
            raise QgsProcessingException('無法建立輸出。')
        folder = Path(self.parameterAsString(parameters, 'CACHE', context))
        source = Source(folder, feedback)
        report = {'finished': False, 'source': API, 'crs': CRS, 'area_m2': area.area(),
                  'range_sha256': hashlib.sha256(bytes(area.asWkb())).hexdigest(),
                  'scope_note': '公開登記建號定位點；非輪廓，非官方核證全量。地段界外100m以外的錯置點可能遺漏。',
                  'observed_response_cap': CAP, 'completed_sections': [], 'point_records': 0,
                  'missing_dates_inside': 0, 'invalid_dates_inside': 0}
        unknown = []
        seen = {}
        try:
            sections, districts, missing = candidates(source, area, feedback)
            if not sections:
                raise QgsProcessingException('範圍未找到高雄候選地段。')
            report.update({'districts': districts, 'candidate_sections': sections, 'sections_without_boundary': missing})
            for index, sec in enumerate(sections):
                feedback.setProgress(100 * index / len(sections))
                feedback.setProgressText(f'{index + 1}/{len(sections)} {sec["name"]}')
                for row in source.buildings(sec['code']):
                    source.check()
                    key = (sec['code'], row['dd49'])
                    signature = (str(row.get('dd16')), str(row.get('x97')), str(row.get('y97')))
                    if key in seen:
                        if seen[key] != signature:
                            raise QgsProcessingException(f'{key} 日期或座標衝突；請以新快取資料夾重抓。')
                        continue
                    seen[key] = signature
                    try:
                        x, y = float(row['x97']), float(row['y97'])
                        if not all(map(math.isfinite, (x, y))) or not (100000 < x < 400000 and 2400000 < y < 2800000):
                            raise ValueError('位置超出台灣 TM2 合理範圍')
                    except (TypeError, ValueError):
                        unknown.append({'section': sec['code'], 'build_no': row['dd49'],
                                        'date_roc': row.get('dd16'), 'x97': row.get('x97'), 'y97': row.get('y97')})
                        continue
                    point = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                    if not engine.intersects(point.constGet()):
                        continue
                    date, status = date_value(row.get('dd16'))
                    record = QgsFeature(fields)
                    record.setGeometry(point)
                    url = API + '?' + urlencode({'SERVICE': 'GETLIST', 'ITEM': 'build', 'DD48': sec['code'],
                                                'DD49': row['dd49'], 'hasGeometry': 'true', 'SRS': ''})
                    record.setAttributes([sec['district'], sec['code'], sec['name'], row['dd49'], str(row.get('dd16') or ''),
                                          date, status, row.get('dd09'), row.get('dd13'), row.get('dd12'), x, y, url, sec['office'],
                                          float(row['dd08']) if re.fullmatch(r'\d+(?:\.\d+)?', str(row.get('dd08', ''))) else None])
                    if not sink.addFeature(record, QgsFeatureSink.FastInsert):
                        raise QgsProcessingException('輸出寫入失敗。')
                    report['point_records'] += 1
                    report['missing_dates_inside'] += status == '缺漏'
                    report['invalid_dates_inside'] += status == '格式或日期無效'
                report['completed_sections'].append(sec['code'])
                feedback.pushInfo(f'目前界內 {report["point_records"]} 筆')
            report['finished'] = True
            feedback.setProgress(100)
        except BaseException as exc:
            report['error'] = str(exc)
            raise
        finally:
            sink.flushBuffer()
            report['candidate_records_seen'] = len(seen)
            report['unlocated_candidate_records'] = len(unknown)
            report['requests_this_run'] = source.requests
            report['run_time_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
            (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            (folder / 'unlocated.json').write_text(json.dumps(unknown, ensure_ascii=False, indent=2), encoding='utf-8')
            source.db.close()
        feedback.pushInfo(f'已輸出 {report["point_records"]} 筆。候選資料缺座標 {len(unknown)} 筆。')
        return {'OUTPUT': destination}
