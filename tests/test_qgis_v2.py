"""Offline integration checks of v2 geometry choices and cadastral relationships."""
import json
import os
import sys
import tempfile
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from qgis.core import *
from kaohsiung_buildings_plugin import range_algorithm as rng
from kaohsiung_buildings_plugin.area_algorithm import AreaBuildingAlgorithm
app=QgsApplication([],False);app.initQgis()
real_source,real_candidates=rng.Source,rng.candidates
# Actual pagination implementation: server uses substring matching and caps at 1000.
rows=[{'aa48':'0335','aa49':f'{i:08d}'} for i in range(1001)]
class PaginationSource(real_source):
    def __init__(self):self.queries=[]
    def check(self):pass
    def listing(self,item,**params):
        assert item=='land';term=params['AA49'];self.queries.append(term)
        return [r for r in rows if term in r['aa49']][:1000]
s=PaginationSource();found=list(s.lands('0335'))
assert len(found)==len({r['aa49'] for r in found})==1001
assert len(s.queries)>1
class BadSource(PaginationSource):
    def listing(self,item,**params):return [{'aa48':'9999','aa49':'00000001'}]
try:list(BadSource().lands('0335'))
except QgsProcessingException:pass
else:raise AssertionError('Wrong section accepted')
class PiecesSource(PaginationSource):
    def listing(self,item,**params):
        return [dict(aa48='0335',aa49='00010000',aa16='100',wkt=w) for w in [
            'POLYGON((0 0,1 0,1 1,0 1,0 0))', 'POLYGON((3 0,4 0,4 1,3 1,3 0))']]
combined=list(PiecesSource().lands('0335'))
assert len(combined)==1 and combined[0]['source_parts']==2
assert QgsGeometry.fromWkt(combined[0]['wkt']).area()==2
class ConflictingPieces(PiecesSource):
    def listing(self,item,**params):
        values=super().listing(item,**params);values[1]['aa16']='200';return values
try:list(ConflictingPieces().lands('0335'))
except QgsProcessingException:pass
else:raise AssertionError('Conflicting prices silently merged')

with tempfile.TemporaryDirectory(prefix='kh-v2-') as directory:
    tmp=Path(directory)
    boundary=QgsVectorLayer('Polygon?crs=EPSG:3826','range','memory')
    for x in [181170,190000]:
        f=QgsFeature();f.setGeometry(QgsGeometry.fromWkt(f'POLYGON(({x} 2515590,{x+20} 2515590,{x+20} 2515610,{x} 2515610,{x} 2515590))'));boundary.dataProvider().addFeatures([f])
    boundary.updateExtents();QgsProject.instance().addMapLayer(boundary)
    boundary.selectByIds([next(boundary.getFeatures()).id()])
    selected=QgsProcessingFeatureSourceDefinition(boundary.id(),selectedFeaturesOnly=True)
    context=QgsProcessingContext();context.setProject(QgsProject.instance())
    class DB:
        def close(self):pass
    class Source:
        cancel=False
        def __init__(self,folder,feedback):folder.mkdir(parents=True,exist_ok=True);self.feedback=feedback;self.db=DB();self.requests=0
        def check(self):
            if self.feedback.isCanceled():raise QgsProcessingException('cancelled')
        def lands(self,section):
            if self.cancel:self.feedback.cancel()
            for n,x in [('00010000',181165),('00020000',181180)]:
                yield {'aa48':section,'aa49':n,'aa48n':'測試段','aa10':'300','aa16':'12000' if n=='00010000' else 'NA','aa17':'0' if n=='00010000' else '',
                       'wkt':f'POLYGON(({x} 2515590,{x+15} 2515590,{x+15} 2515610,{x} 2515610,{x} 2515590))'}
        def buildings(self,section):
            # Two registered buildings on land 1, one positioned outside that parcel and area.
            for n,land,x,date in [('00690000','00010000',181175,'1021015'),('00690001','00010000',190010,'1020230'),('00690002','00990000',181185,'')]:
                yield {'dd48':section,'dd49':n,'aa48':section,'aa49':land,'dd16':date,'x97':str(x),'y97':'2515600','dd08':'212.13','dd09':'測試地址','dd13':'004','dd12':'RC'}
    def candidates(source,area,feedback):
        assert abs(area.area()-400)<1e-6,'unselected geometry included'
        return [{'code':'0335','name':'測試段','district':'楠梓區','office':'EE'}],[],[]
    rng.Source=Source;rng.candidates=candidates
    alg=AreaBuildingAlgorithm();alg.initAlgorithm()
    common={'RANGE':selected,'CACHE':str(tmp/'cache'),'MODE':1,'CLIP':True,'FULL':False}
    result=alg.processAlgorithm({**common,'OUTPUT':str(tmp/'land.gpkg'),'BUILDINGS':str(tmp/'build.gpkg')},context,QgsProcessingFeedback())
    land=QgsVectorLayer(result['OUTPUT'],'land','ogr');build=QgsVectorLayer(result['BUILDINGS'],'build','ogr')
    assert land.featureCount()==2 and build.featureCount()==2 and land.geometryType()==QgsWkbTypes.PolygonGeometry
    fs={f['land_no']:f for f in land.getFeatures()};one,two=fs['00010000'],fs['00020000']
    assert one['build_count']==2 and two['build_count']==0
    assert abs(one.geometry().area()-200)<1e-6 and one['geom_area_m2']==300
    assert one['ann_value']==12000 and one['ann_price']==0
    assert QgsVariantUtils.isNull(two['ann_value']) and QgsVariantUtils.isNull(two['ann_price'])
    assert QgsVariantUtils.isNull(one['value_year']) and QgsVariantUtils.isNull(one['price_year'])
    assert one['finish_min'].toString('yyyy-MM-dd')=='2013-10-15' and one['date_unknown']==1
    assert len(json.loads(one['buildings_json']))==2
    assert {f['parcel_key'] for f in build.getFeatures()}=={one['parcel_key']}
    assert any(f['point_on_land']==0 for f in build.getFeatures())
    report=json.loads((tmp/'cache/polygon_report.json').read_text());assert report['finished'] and report['unmatched_diagnostic_records']==1
    # Full parcels can extend outside the selection; point mode outputs only inside points.
    r=alg.processAlgorithm({**common,'CLIP':False,'OUTPUT':str(tmp/'full.gpkg')},context,QgsProcessingFeedback())
    full=QgsVectorLayer(r['OUTPUT'],'full','ogr');assert all(abs(f.geometry().area()-300)<1e-6 for f in full.getFeatures())
    r=alg.processAlgorithm({**common,'MODE':0,'OUTPUT':str(tmp/'points.gpkg')},context,QgsProcessingFeedback())
    points=QgsVectorLayer(r['OUTPUT'],'points','ogr');assert points.featureCount()==2 and points.geometryType()==QgsWkbTypes.PointGeometry
    assert points.fields().indexFromName('ann_value')==-1 and points.fields().indexFromName('land_no')==-1
    # Reject incompatible options before creating outputs or querying.
    try:alg.processAlgorithm({**common,'FULL':True,'OUTPUT':str(tmp/'bad.gpkg')},context,QgsProcessingFeedback())
    except QgsProcessingException:pass
    else:raise AssertionError('Polygon FULL option silently ignored')
    Source.cancel=True
    try:alg.processAlgorithm({**common,'OUTPUT':str(tmp/'cancel.gpkg')},context,QgsProcessingFeedback())
    except QgsProcessingException:pass
    else:raise AssertionError('Cancellation ignored')
    assert not json.loads((tmp/'cache/polygon_report.json').read_text())['finished']
print('PASS: capped substring enumeration; selected range; polygon/point modes; clipping; exact land IDs; many buildings per parcel; source prices, zero and missing values; dates; cancellation')
