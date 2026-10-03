"""Direct parcel vectors and ID-based building links; QGIS and standard library only."""
import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
    QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessing,
    QgsProcessingException, QgsWkbTypes,
)
from . import range_algorithm as rng


def number(raw):
    try:
        value = float(raw)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def polygon_geometry(geometry):
    """Keep polygon parts of an intersection, including geometry collections."""
    if geometry.isNull() or geometry.isEmpty():
        return QgsGeometry()
    if geometry.type() == QgsWkbTypes.PolygonGeometry:
        result = QgsGeometry(geometry)
        result.convertToMultiType()
        return result
    if QgsWkbTypes.flatType(geometry.wkbType()) == QgsWkbTypes.GeometryCollection:
        parts = [polygon_geometry(g) for g in geometry.asGeometryCollection()]
        parts = [g for g in parts if not g.isEmpty()]
        return polygon_geometry(QgsGeometry.unaryUnion(parts)) if parts else QgsGeometry()
    return QgsGeometry()


def fields(spec):
    result = QgsFields()
    for name, kind in spec:
        result.append(QgsField(name, kind))
    return result


S, I, D, DATE = QVariant.String, QVariant.Int, QVariant.Double, QVariant.Date
LAND_FIELDS = [
    ('parcel_key', S), ('office', S), ('section', S), ('sect_name', S),
    ('land_no', S), ('display_no', S), ('district', S), ('reg_area_m2', D),
    ('geom_area_m2', D), ('inside_area_m2', D), ('ann_value', D), ('ann_price', D),
    ('value_raw', S), ('price_raw', S), ('value_year', I), ('price_year', I),
    ('price_unit', S), ('price_note', S), ('build_count', I), ('build_refs', S),
    ('finish_dates', S), ('finish_min', DATE), ('finish_max', DATE), ('date_unknown', I),
    ('build_status', S), ('buildings_json', S), ('repaired', I), ('source_parts', I), ('source_url', S),
]
BUILD_FIELDS = [
    ('building_key', S), ('parcel_key', S), ('office', S), ('section', S), ('build_no', S),
    ('land_section', S), ('land_no', S), ('date_roc', S), ('finish_date', DATE),
    ('date_state', S), ('address', S), ('floors', S), ('main_floor', S),
    ('structure', S), ('area_m2', D), ('x97', D), ('y97', D),
    ('point_on_land', I), ('point_in_range', I), ('source_url', S),
]


def process_parcels(algorithm, parameters, context, feedback):
    boundary = algorithm.parameterAsSource(parameters, 'RANGE', context)
    if boundary is None or not boundary.sourceCrs().isValid():
        raise QgsProcessingException('請選有有效座標系統的範圍多邊形。')
    crs = QgsCoordinateReferenceSystem(rng.CRS)
    transform = QgsCoordinateTransform(boundary.sourceCrs(), crs, context.transformContext())
    pieces = []
    for feature in boundary.getFeatures():
        if feedback.isCanceled():
            raise QgsProcessingException('已取消。')
        g = QgsGeometry(feature.geometry())
        if g.isEmpty():
            continue
        g.transform(transform)
        if not g.isGeosValid():
            g = g.makeValid()
        g = polygon_geometry(g)
        if not g.isEmpty():
            pieces.append(g)
    area = QgsGeometry.unaryUnion(pieces)
    if area.isEmpty() or not area.isGeosValid():
        raise QgsProcessingException('範圍幾何無效或為空。')
    clip = algorithm.parameterAsBool(parameters, 'CLIP', context)
    folder = Path(algorithm.parameterAsString(parameters, 'CACHE', context))
    source = rng.Source(folder, feedback)
    report = {'finished': False, 'mode': 'Polygon', 'clipped': clip, 'crs': rng.CRS,
              'range_sha256': hashlib.sha256(bytes(area.asWkb())).hexdigest(),
              'study_area_m2': area.area(), 'completed_land_sections': [],
              'completed_building_sections': [], 'missing_geometry': [],
              'parcels': 0, 'building_records': 0, 'source': rng.API,
              'announcement_year': '接口未提供；年度留空',
              'scope_note': '依來源土地地段＋地號關聯；非完整法定坐落地號清冊。未找到建號不代表沒有建物；快取可能較早。'}
    parcels, building_rows, unmatched = {}, [], []
    sink = table = None
    try:
        sections, districts, missing = rng.candidates(source, area, feedback)
        if not sections:
            raise QgsProcessingException('範圍未找到高雄候選地段。')
        report.update(candidate_sections=sections, districts=districts, sections_without_boundary=missing)
        for index, sec in enumerate(sections):
            source.check()
            feedback.setProgressText(f'宗地 {index+1}/{len(sections)}：{sec["name"]}')
            feedback.setProgress(40 * index / len(sections))
            for row in source.lands(sec['code']):
                source.check()
                key = (row['aa48'], row['aa49'])
                g = QgsGeometry.fromWkt(row.get('wkt') or '')
                if g.isEmpty():
                    report['missing_geometry'].append({'section': key[0], 'land_no': key[1]})
                    continue
                repaired = int(not g.isGeosValid())
                if repaired:
                    g = g.makeValid()
                g = polygon_geometry(g)
                if g.isEmpty() or not g.isGeosValid():
                    raise QgsProcessingException(f'{key} 無有效宗地圖形，停止並保留查詢快取。')
                if not g.boundingBox().intersects(area.boundingBox()):
                    continue
                intersection = g.intersection(area)
                if intersection.lastError():
                    raise QgsProcessingException(f'{key} 裁切失敗：{intersection.lastError()}')
                intersection = polygon_geometry(intersection)
                if intersection.isEmpty() or intersection.area() <= 1e-8:
                    continue
                if not intersection.isGeosValid():
                    raise QgsProcessingException(f'{key} 裁切後圖形無效。')
                if key in parcels:
                    raise QgsProcessingException(f'重複宗地識別碼：{key}')
                parcels[key] = {'row': row, 'sec': sec, 'full': g, 'clip': intersection,
                                'repaired': repaired, 'key': '|'.join((sec['office'], *key))}
            report['completed_land_sections'].append(sec['code'])
        links, seen = defaultdict(list), {}
        for index, sec in enumerate(sections):
            feedback.setProgressText(f'建號 {index+1}/{len(sections)}：{sec["name"]}')
            feedback.setProgress(40 + 40 * index / len(sections))
            for row in source.buildings(sec['code']):
                source.check()
                key = (row['dd48'], row['dd49'])
                if key in seen:
                    if seen[key] != row:
                        raise QgsProcessingException(f'{key} 建號資料衝突，請用新快取重查。')
                    continue
                seen[key] = row
                land = (row.get('aa48'), row.get('aa49'))
                x, y = number(row.get('x97')), number(row.get('y97'))
                point = None
                if x is not None and y is not None and 100000 < x < 400000 and 2400000 < y < 2800000:
                    point = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                in_range = int(area.intersects(point)) if point is not None else None
                if land not in parcels:
                    if in_range or point is None:
                        unmatched.append({'section': key[0], 'build_no': key[1], 'land_section': land[0],
                                          'land_no': land[1], 'x97': x, 'y97': y, 'point_in_range': in_range})
                    continue
                date, state = rng.date_value(row.get('dd16'))
                building = {'building_key': '|'.join((sec['office'], *key)), 'parcel_key': parcels[land]['key'],
                            'office': sec['office'], 'section': key[0], 'build_no': key[1],
                            'land_section': land[0], 'land_no': land[1],
                            'date_roc': str(row.get('dd16') or ''),
                            'finish_date': date.toString('yyyy-MM-dd') if date is not None else None,
                            'date_state': state, 'address': row.get('dd09'), 'floors': row.get('dd13'),
                            'main_floor': row.get('dd_main_floor'), 'structure': row.get('dd12'),
                            'area_m2': number(row.get('dd08')), 'x97': x, 'y97': y,
                            'point_on_land': int(parcels[land]['full'].intersects(point)) if point is not None else None,
                            'point_in_range': in_range,
                            'source_url': rng.API + '?' + urlencode({'SERVICE': 'GETLIST', 'ITEM': 'build',
                                                                   'DD48': key[0], 'DD49': key[1], 'hasGeometry': 'false', 'SRS': ''})}
                links[land].append(building)
                building_rows.append(building)
            report['completed_building_sections'].append(sec['code'])
        source.check()
        lf, bf = fields(LAND_FIELDS), fields(BUILD_FIELDS)
        sink, destination = algorithm.parameterAsSink(parameters, 'OUTPUT', context, lf, QgsWkbTypes.MultiPolygon, crs)
        table_parameters = dict(parameters)
        table_parameters['BUILDINGS'] = parameters.get('BUILDINGS') or QgsProcessing.TEMPORARY_OUTPUT
        table, table_id = algorithm.parameterAsSink(table_parameters, 'BUILDINGS', context, bf,
                                                   QgsWkbTypes.NoGeometry, QgsCoordinateReferenceSystem())
        if sink is None or table is None:
            raise QgsProcessingException('無法建立宗地或建號明細輸出。')
        def write(target, schema, values, geometry=None):
            feature = QgsFeature(schema)
            if geometry is not None:
                feature.setGeometry(geometry)
            attrs = []
            for field in schema:
                value = values.get(field.name())
                if field.type() == DATE and value:
                    from qgis.PyQt.QtCore import QDate
                    value = QDate.fromString(value, 'yyyy-MM-dd')
                attrs.append(value)
            feature.setAttributes(attrs)
            if not target.addFeature(feature, QgsFeatureSink.FastInsert):
                raise QgsProcessingException('輸出寫入失敗。')
        for key, parcel in sorted(parcels.items()):
            source.check()
            row, sec = parcel['row'], parcel['sec']
            buildings = sorted(links[key], key=lambda b: b['building_key'])
            dates = sorted({b['finish_date'] for b in buildings if b['finish_date']})
            land_no = key[1]
            values = {'parcel_key': parcel['key'], 'office': sec['office'], 'section': key[0],
                      'sect_name': row.get('aa48n', sec['name']), 'land_no': land_no,
                      'display_no': str(int(land_no[:4])) + ('-'+str(int(land_no[4:])) if int(land_no[4:]) else ''),
                      'district': sec['district'], 'reg_area_m2': number(row.get('aa10')),
                      'geom_area_m2': parcel['full'].area(), 'inside_area_m2': parcel['clip'].area(),
                      'ann_value': number(row.get('aa16')), 'ann_price': number(row.get('aa17')),
                      'value_raw': str(row.get('aa16') or ''), 'price_raw': str(row.get('aa17') or ''),
                      'price_unit': '新臺幣元／平方公尺', 'price_note': '接口未提供公告年度；查詢日期不代表公告年度',
                      'build_count': len(buildings), 'build_refs': '；'.join(b['section']+':'+b['build_no'] for b in buildings),
                      'finish_dates': '；'.join(dates), 'finish_min': dates[0] if dates else None,
                      'finish_max': dates[-1] if dates else None,
                      'date_unknown': sum(b['finish_date'] is None for b in buildings),
                      'build_status': '已對應來源建號' if buildings else '本次來源未找到對應建號',
                      'buildings_json': json.dumps(buildings, ensure_ascii=False), 'repaired': parcel['repaired'],
                      'source_parts': row.get('source_parts', 1),
                      'source_url': rng.API+'?'+urlencode({'SERVICE':'GETLIST','ITEM':'land','AA48':key[0],
                                                        'AA49':key[1],'hasGeometry':'true','SRS':''})}
            write(sink, lf, values, parcel['clip'] if clip else parcel['full'])
            report['parcels'] += 1
        for building in building_rows:
            source.check()
            write(table, bf, building)
            report['building_records'] += 1
        source.check()
        shapes = [p['clip'] for p in parcels.values()]
        union = QgsGeometry.unaryUnion(shapes) if shapes else QgsGeometry()
        if shapes and (union.isEmpty() or union.lastError()):
            raise QgsProcessingException('宗地聯集檢查失敗，請核對部分輸出。')
        covered = union.area() if shapes else 0
        report.update(finished=True, parcels_with_buildings=sum(bool(v) for v in links.values()),
                      coverage_pct=100*covered/area.area(), uncovered_m2=max(0, area.area()-covered),
                      overlap_sum_minus_union_m2=max(0, sum(g.area() for g in shapes)-covered),
                      ann_value_missing=sum(number(p['row'].get('aa16')) is None for p in parcels.values()),
                      ann_price_missing=sum(number(p['row'].get('aa17')) is None for p in parcels.values()))
        feedback.setProgress(100)
        feedback.pushInfo(f'已輸出 {len(parcels)} 幅宗地、{len(building_rows)} 筆建號明細。請自行核對資料與位置。')
        return {'OUTPUT': destination, 'BUILDINGS': table_id}
    except BaseException as exc:
        report['error'] = str(exc) or '已取消'
        raise
    finally:
        if sink is not None:
            sink.flushBuffer()
        if table is not None:
            table.flushBuffer()
        source.db.close()
        report.update(requests_this_run=source.requests, unmatched_diagnostic_records=len(unmatched),
                      run_time_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        (folder/'polygon_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (folder/'polygon_unmatched_buildings.json').write_text(json.dumps(unmatched,ensure_ascii=False,indent=2),encoding='utf-8')
