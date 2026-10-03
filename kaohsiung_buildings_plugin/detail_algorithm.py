import datetime as dt
import json
import re
import sqlite3
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsProcessing,
    QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink, QgsProcessingParameterField,
    QgsProcessingParameterFile, QgsProcessingParameterString, QgsWkbTypes,
    QgsCoordinateReferenceSystem, QgsVariantUtils,
)
from .range_algorithm import date_value

DETAIL_URL = 'https://easymap.moi.gov.tw/Z10Web/BuildDesc_ajax_detail'


class DetailParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.row, self.cell, self.rows = [], None, []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.ignored += 1
        if tag == 'tr':
            self.row = []
        elif tag in ('th', 'td'):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None and not self.ignored:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.ignored = max(0, self.ignored - 1)
        if tag in ('th', 'td') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row:
            self.rows.append(self.row)
            self.row = []


def parse_detail(html, section, number):
    parser = DetailParser()
    parser.feed(html)
    basic, components = {}, []
    in_components = False
    for row in parser.rows:
        label = row[0]
        if label == '建物資訊':
            in_components = True
        elif label == '分享':
            in_components = False
        elif len(row) == 2:
            if in_components:
                components.append({'name': label, 'raw': row[1], 'area_m2': area_number(row[1])})
            else:
                basic[label] = row[1]
    if basic.get('建號') != number or not basic.get('地段', '').startswith(section + ' '):
        raise ValueError('回傳資料的地段／建號不符，或網站沒有提供該建物資料。')
    if '建物完成日期' not in basic:
        raise ValueError('回傳缺少完成日期欄位，網站格式可能改變。')
    return {'basic': basic, 'components': components}


def area_number(text):
    match = re.match(r'^\s*(\d[\d,]*(?:\.\d+)?)\s*(?:\(|平方公尺|$)', str(text))
    return float(match[1].replace(',', '')) if match else None


def field_text(value):
    return '' if QgsVariantUtils.isNull(value) else str(value).strip()


class DetailClient:
    def __init__(self, folder, feedback):
        folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(folder / 'building_detail_cache.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS details (key TEXT PRIMARY KEY, payload TEXT)')
        self.feedback, self.last = feedback, 0.0

    def check(self):
        if self.feedback.isCanceled():
            raise QgsProcessingException('已取消；明細快取已保留，可續跑。')

    def get(self, office, section, number):
        self.check()
        key = '|'.join((office, section, number))
        cached = self.db.execute('SELECT payload FROM details WHERE key=?', (key,)).fetchone()
        if cached:
            return json.loads(cached[0])
        url = DETAIL_URL + '?' + urlencode({'office': office, 'sectNo': section, 'buildNo': number})
        for attempt in range(3):
            self.check()
            delay = max(0, 1.0 - (time.monotonic() - self.last))
            time.sleep(delay)
            self.last = time.monotonic()
            try:
                with urlopen(Request(url, headers={'User-Agent':'QGIS-KHBuildings/1.0'}), timeout=45) as response:
                    html = response.read().decode('utf-8-sig')
                data = parse_detail(html, section, number)
                data.update({'url': url, 'fetched_utc': dt.datetime.now(dt.timezone.utc).isoformat()})
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO details VALUES (?,?)', (key, json.dumps(data, ensure_ascii=False)))
                return data
            except HTTPError as exc:
                if exc.code in (401, 403, 429):
                    raise QgsProcessingException(f'網站 HTTP {exc.code}，已停止；請稍後用同一快取續跑。') from exc
                if exc.code < 500:
                    raise ValueError(f'網站 HTTP {exc.code}') from exc
                if attempt == 2:
                    raise QgsProcessingException('網站持續回報伺服器錯誤，已停止並保留快取。') from exc
            except (URLError, TimeoutError) as exc:
                if attempt == 2:
                    raise QgsProcessingException('連線失敗，已停止並保留快取。') from exc
            time.sleep(2 ** attempt)


EXTRA = [
    ('building_key', QVariant.String), ('moi_office', QVariant.String),
    ('moi_district', QVariant.String), ('moi_office_name', QVariant.String),
    ('moi_section', QVariant.String), ('moi_area_m2', QVariant.Double),
    ('moi_area_raw', QVariant.String), ('moi_floors', QVariant.String),
    ('moi_date_roc', QVariant.String), ('moi_finish', QVariant.Date),
    ('moi_date_text', QVariant.String), ('moi_use', QVariant.String),
    ('moi_state', QVariant.String), ('moi_url', QVariant.String), ('moi_fetched', QVariant.String),
]


class BuildingDetailsAlgorithm(QgsProcessingAlgorithm):
    def name(self): return 'building_details'
    def displayName(self): return '補齊建物資料與樓層面積（既有定位點）'
    def group(self): return '建物資料'
    def groupId(self): return 'building_dates'
    def createInstance(self): return BuildingDetailsAlgorithm()
    def shortHelpString(self):
        return ('用事務所＋地段＋建號逐筆查詢，保留原始點位置及欄位。輸出基本資料點圖層及一對多樓層面積表，'
                '兩者以 building_key 連接。若有 office 欄會自動採用；舊楠梓圖層可用備用代碼 EE。'
                '完整資料每個建號需一次查詢，26,896筆單計1秒間隔已約7.5小時，另加網站回應時間。'
                '可只選部分點先試；取消後快取保留。來源缺項留空，單筆查無資料會標記，不刪除原位置。')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource('INPUT', '建物定位點', [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField('SECTION', '地段代碼欄位', defaultValue='section', parentLayerParameterName='INPUT'))
        self.addParameter(QgsProcessingParameterField('NUMBER', '建號欄位', defaultValue='build_no', parentLayerParameterName='INPUT'))
        self.addParameter(QgsProcessingParameterField('OFFICE_FIELD', '事務所代碼欄位（留空自動用 office 欄）', parentLayerParameterName='INPUT', optional=True))
        self.addParameter(QgsProcessingParameterString('OFFICE', '無事務所欄時的備用代碼（楠梓為 EE）', defaultValue='EE', optional=True))
        self.addParameter(QgsProcessingParameterFile('CACHE', '快取資料夾', behavior=QgsProcessingParameterFile.Folder))
        self.addParameter(QgsProcessingParameterFeatureSink('OUTPUT', '完整建物資料定位點', QgsProcessing.TypeVectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink('TABLE', '樓層／附屬建物面積表', QgsProcessing.TypeVector))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, 'INPUT', context)
        if source is None:
            raise QgsProcessingException('請選建物點圖層。')
        section_field = self.parameterAsString(parameters, 'SECTION', context)
        number_field = self.parameterAsString(parameters, 'NUMBER', context)
        office_field = self.parameterAsString(parameters, 'OFFICE_FIELD', context)
        if not office_field and source.fields().indexFromName('office') >= 0:
            office_field = 'office'
        for name in [section_field, number_field] + ([office_field] if office_field else []):
            if not name or source.fields().indexFromName(name) < 0:
                raise QgsProcessingException(f'找不到欄位：{name}')
        fallback = self.parameterAsString(parameters, 'OFFICE', context).strip().upper()
        fields = QgsFields(source.fields())
        if any(fields.indexFromName(name) >= 0 for name, _ in EXTRA):
            raise QgsProcessingException('輸入已含完整資料欄位；請改選原始定位點圖層，避免覆蓋。')
        for name, typ in EXTRA:
            fields.append(QgsField(name, typ))
        sink, output = self.parameterAsSink(parameters, 'OUTPUT', context, fields, source.wkbType(), source.sourceCrs())
        parts = QgsFields()
        for name, typ in [('building_key',QVariant.String),('office',QVariant.String),('section',QVariant.String),
                          ('build_no',QVariant.String),('part_order',QVariant.Int),('part_name',QVariant.String),
                          ('area_m2',QVariant.Double),('area_raw',QVariant.String)]:
            parts.append(QgsField(name, typ))
        table, table_id = self.parameterAsSink(parameters, 'TABLE', context, parts, QgsWkbTypes.NoGeometry, QgsCoordinateReferenceSystem())
        if sink is None or table is None:
            raise QgsProcessingException('無法建立輸出圖層／明細表。')
        folder = Path(self.parameterAsString(parameters, 'CACHE', context))
        client = DetailClient(folder, feedback)
        report = {'finished':False, 'processed':0, 'success':0, 'failed':0, 'component_rows':0, 'errors':[]}
        part_keys = set()
        try:
            for index, feature in enumerate(source.getFeatures()):
                client.check()
                sec = field_text(feature[section_field])
                number = field_text(feature[number_field])
                office = field_text(feature[office_field]).upper() if office_field else fallback
                sec = sec.zfill(4) if sec.isdigit() else sec
                number = number.zfill(8) if number.isdigit() else number
                key = '|'.join((office,sec,number))
                values = {name:None for name,_ in EXTRA}
                values.update({'building_key':key,'moi_office':office})
                try:
                    if not re.fullmatch(r'[A-Z0-9]{2}',office) or not re.fullmatch(r'\d{4}',sec) or not re.fullmatch(r'\d{8}',number):
                        raise ValueError('事務所、地段或建號格式不正確；沒有猜測代碼。')
                    data = client.get(office,sec,number)
                    basic = data['basic']
                    raw_date = basic.get('建物完成日期','')
                    match = re.match(r'^(\d{7})(?!\d)',raw_date)
                    roc = match[1] if match else raw_date
                    date, status = date_value(roc)
                    values.update({'moi_district':basic.get('行政區'),'moi_office_name':basic.get('地政事務所'),
                                   'moi_section':basic.get('地段'),'moi_area_m2':area_number(basic.get('建物面積','')),
                                   'moi_area_raw':basic.get('建物面積'),'moi_floors':basic.get('樓層數'),
                                   'moi_date_roc':roc,'moi_finish':date,'moi_date_text':raw_date,
                                   'moi_use':basic.get('主要用途'),'moi_state':'成功；日期'+status,
                                   'moi_url':data['url'],'moi_fetched':data['fetched_utc']})
                    if key not in part_keys:
                        for order, component in enumerate(data['components'],1):
                            part = QgsFeature(parts)
                            part.setAttributes([key,office,sec,number,order,component['name'],component['area_m2'],component['raw']])
                            if not table.addFeature(part,QgsFeatureSink.FastInsert):
                                raise QgsProcessingException('面積明細寫入失敗。')
                            report['component_rows'] += 1
                        part_keys.add(key)
                    report['success'] += 1
                except ValueError as exc:
                    values['moi_state'] = '查詢失敗：'+str(exc)
                    report['failed'] += 1
                    report['errors'].append({'building_key':key,'error':str(exc)})
                    feedback.reportError(key+': '+str(exc),False)
                result = QgsFeature(fields)
                result.setGeometry(feature.geometry())
                result.setAttributes(feature.attributes()+[values[name] for name,_ in EXTRA])
                if not sink.addFeature(result,QgsFeatureSink.FastInsert):
                    raise QgsProcessingException('建物資料寫入失敗。')
                report['processed'] += 1
                feedback.setProgress(100*(index+1)/max(1,source.featureCount()))
                feedback.setProgressText(f'已補資料 {index+1}/{source.featureCount()}：{key}')
            report['finished'] = True
        except BaseException as exc:
            report['error'] = str(exc) or '已取消'
            raise
        finally:
            sink.flushBuffer()
            table.flushBuffer()
            client.db.close()
            report['run_time_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
            (folder/'detail_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return {'OUTPUT':output,'TABLE':table_id}
