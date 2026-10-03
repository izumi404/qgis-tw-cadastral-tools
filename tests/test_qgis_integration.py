import sys
import json
import copy
import tempfile
import os
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qgis.core import *
from qgis.PyQt.QtWidgets import QMainWindow
from qgis.PyQt.QtCore import QVariant
app=QgsApplication([],False)
app.initQgis()
sys.path.append(str(Path(QgsApplication.pkgDataPath()) / "python" / "plugins"))
import kaohsiung_buildings_plugin as plugin
from kaohsiung_buildings_plugin import range_algorithm as rng
from kaohsiung_buildings_plugin import detail_algorithm as det
from kaohsiung_buildings_plugin.area_algorithm import AreaBuildingAlgorithm

temporary=tempfile.TemporaryDirectory(prefix='khbuildings-test-')
ROOT=Path(temporary.name)
fixture=(REPO/'tests/fixtures/building_detail.html').read_text(encoding='utf-8')
parsed=det.parse_detail(fixture,'0335','00690000')
assert len(parsed['components'])==7
assert parsed['basic']['建物面積']=='212.13 平方公尺'
assert parsed['components'][-1]['area_m2']==12.76
try:det.parse_detail(fixture,'0335','00000001')
except ValueError:pass
else:raise AssertionError('identity mismatch accepted')

# A source layer with two polygons, only one selected.
polygons=QgsVectorLayer('Polygon?crs=EPSG:3826','test selection','memory')
features=[]
for x in [181170,190000]:
    f=QgsFeature();f.setGeometry(QgsGeometry.fromWkt(f'POLYGON(({x} 2515590,{x+20} 2515590,{x+20} 2515610,{x} 2515610,{x} 2515590))'))
    features.append(f)
polygons.dataProvider().addFeatures(features);polygons.updateExtents()
QgsProject.instance().addMapLayer(polygons)
polygons.selectByIds([next(polygons.getFeatures()).id()])

class Iface:
    def __init__(self):self.window=QMainWindow();self.menus=[];self.toolbar=[]
    def mainWindow(self):return self.window
    def activeLayer(self):return polygons
    def addPluginToMenu(self,m,a):self.menus.append(a)
    def removePluginMenu(self,m,a):self.menus.remove(a)
    def addToolBarIcon(self,a):self.toolbar.append(a)
    def removeToolBarIcon(self,a):self.toolbar.remove(a)
iface=Iface();instance=plugin.classFactory(iface)
instance.initProcessing();instance.initGui();instance.initGui()
assert len(iface.menus)==3 and len(iface.toolbar)==2
assert len(instance.provider.algorithms())==2
import processing
captured=[]
original_dialog=processing.execAlgorithmDialog
processing.execAlgorithmDialog=lambda name,params:captured.append((name,params))
instance.open_range()
definition=captured[0][1]['RANGE']
assert definition.selectedFeaturesOnly is True
processing.execAlgorithmDialog=original_dialog
instance.unload();assert not iface.menus and not iface.toolbar
assert QgsApplication.processingRegistry().providerById('khbuildings') is None

class FakeDB:
    def close(self):pass
class FakeRangeSource:
    def __init__(self,folder,feedback):folder.mkdir(parents=True,exist_ok=True);self.db=FakeDB();self.requests=0
    def check(self):pass
    def buildings(self,section):
        for number,x in [('00690000',181180),('00001000',190010)]:
            yield {'dd48':'0335','dd49':number,'dd16':'1021015','x97':str(x),'y97':'2515600','dd08':'212.13','dd13':'004'}
rng.Source=FakeRangeSource
def fake_candidates(source,area,feedback):
    assert abs(area.area()-400)<.01,'unselected polygon was included'
    return [{'code':'0335','name':'楠梓段一小段','district':'楠梓區','office':'EE'}],[],[]
rng.candidates=fake_candidates

class FakeDetailClient:
    def __init__(self,folder,feedback):folder.mkdir(parents=True,exist_ok=True);self.db=FakeDB();self.feedback=feedback
    def check(self):
        if self.feedback.isCanceled():raise QgsProcessingException('cancelled')
    def get(self,office,section,number):
        assert (office,section,number)==('EE','0335','00690000')
        return {**copy.deepcopy(parsed),'url':'https://easymap.moi.gov.tw/Z10Web/BuildDesc_ajax_detail?office=EE&sectNo=0335&buildNo=00690000','fetched_utc':'2026-10-03T00:00:00+00:00'}
det.DetailClient=FakeDetailClient
context=QgsProcessingContext();context.setProject(QgsProject.instance())
alg=AreaBuildingAlgorithm();alg.initAlgorithm()
result=alg.processAlgorithm({'RANGE':definition,'CACHE':str(ROOT/'cache'),'FULL':True,
    'OUTPUT':str(ROOT/'full.gpkg'),'TABLE':str(ROOT/'parts.gpkg')},context,QgsProcessingFeedback())
points=QgsVectorLayer(result['OUTPUT'],'full','ogr');parts=QgsVectorLayer(result['TABLE'],'parts','ogr')
assert points.featureCount()==1 and parts.featureCount()==7
f=next(points.getFeatures())
assert f['building_key']=='EE|0335|00690000'
assert f['moi_area_m2']==212.13 and f['moi_use']=='住家用'
assert f['moi_finish'].toString('yyyy-MM-dd')=='2013-10-15'
assert f.geometry().asPoint().x()==181180
assert {r['building_key'] for r in parts.getFeatures()}=={f['building_key']}
assert json.loads((ROOT/'cache/detail_report.json').read_text())['finished']

# Failure and duplicate handling: preserve both point records but do not duplicate components.
dup=QgsVectorLayer('Point?crs=EPSG:3826','duplicates','memory')
dup.dataProvider().addAttributes([QgsField('section',QVariant.String),QgsField('build_no',QVariant.String)])
dup.updateFields()
for n in ['00690000','00690000','BAD']:
    f=QgsFeature(dup.fields());f.setAttributes(['0335',n]);f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(181180,2515600)));dup.dataProvider().addFeatures([f])
alg2=det.BuildingDetailsAlgorithm();alg2.initAlgorithm()
r=alg2.processAlgorithm({'INPUT':dup,'SECTION':'section','NUMBER':'build_no','OFFICE':'EE','OFFICE_FIELD':'',
  'CACHE':str(ROOT/'dup_cache'),'OUTPUT':str(ROOT/'dup.gpkg'),'TABLE':str(ROOT/'dup_parts.gpkg')},context,QgsProcessingFeedback())
assert QgsVectorLayer(r['OUTPUT'],'dup','ogr').featureCount()==3
assert QgsVectorLayer(r['TABLE'],'dup_parts','ogr').featureCount()==7
report=json.loads((ROOT/'dup_cache/detail_report.json').read_text());assert report['failed']==1 and report['success']==2
print('PASS: plugin lifecycle, two registered tools, selected polygon only, automatic EE/0335, complete fields, geometry preservation, seven detail rows, foreign key linkage, failure preservation, duplicate details')
