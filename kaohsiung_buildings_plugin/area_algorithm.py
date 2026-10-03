from qgis.core import (QgsProcessing, QgsProcessingParameterBoolean,
                       QgsProcessingParameterFeatureSink, QgsProcessingUtils,
                       QgsProcessingParameterEnum, QgsProcessingException)
from .range_algorithm import BuildingDatesAlgorithm
from .detail_algorithm import BuildingDetailsAlgorithm


class AreaBuildingAlgorithm(BuildingDatesAlgorithm):
    def name(self): return 'polygon_buildings'
    def displayName(self): return '依選取範圍取得資料（Polygon 土地＋建物／Point 建物）'
    def createInstance(self): return AreaBuildingAlgorithm()

    def shortHelpString(self):
        return ('版本 2.0：輸出類型可選 Point（只有建物定位點）或 Polygon（宗地＋土地及建物資料）。'
                'Polygon 使用圖台直接宗地向量，附公告現值、公告地價、建號及日期摘要、逐筆建物 JSON 與一對多建號明細表；不做平滑。'
                'Point 按定位點在範圍內篩選；Polygon 按宗地相交篩選，預設裁切至範圍，建號按來源土地地段＋地號對應。'
                '先選 polygon 再開工具，會自動啟用只處理已選取圖徵。亦可在範圍欄自行選圖層並勾「只處理已選取圖徵」。'
                '自動查相交行政區、地段及事務所代碼，再按所選模式篩選。只支援高雄。'
                '預設下載日期、地址、樓層數、構造、面積及座標；Point 模式勾「完整資料」會再逐建號取得內政部基本資料和各層／附屬面積。'
                '完整資料每建號需一次請求，26,896筆單計1秒間隔約7.5小時，另加網站回應時間。'
                '建議先用小範圍測試。快取可續跑，更新資料請用新空資料夾。定位點不是建物輪廓，完整性未經官方總數核證。')

    def initAlgorithm(self, config=None):
        super().initAlgorithm(config)
        self.removeParameter('OUTPUT')
        self.addParameter(QgsProcessingParameterEnum('MODE', '輸出類型', options=['Point：只有建物定位點', 'Polygon：土地＋建物資料'], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean('CLIP', 'Polygon：裁切至選取範圍（取消則保留完整宗地）', defaultValue=True))
        self.addParameter(QgsProcessingParameterFeatureSink('OUTPUT', '主要輸出（建議 GeoPackage）', QgsProcessing.TypeVectorAnyGeometry))
        self.addParameter(QgsProcessingParameterFeatureSink('BUILDINGS', 'Polygon：對應建號明細表（請另存以保留）', QgsProcessing.TypeVector, optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterBoolean('FULL', 'Point 完整資料：另取主要用途及每層／附屬面積（較慢）', defaultValue=False))
        self.addParameter(QgsProcessingParameterFeatureSink('TABLE', '樓層面積表（勾完整資料時產生）',
                                                           QgsProcessing.TypeVector, optional=True))

    def processAlgorithm(self, parameters, context, feedback):
        if self.parameterAsEnum(parameters, 'MODE', context) == 1:
            if self.parameterAsBool(parameters, 'FULL', context):
                raise QgsProcessingException('「完整資料」只適用 Point 模式；Polygon 已含建物基本資料，請取消此勾選。')
            from .parcel_algorithm import process_parcels
            return process_parcels(self, parameters, context, feedback)
        if not self.parameterAsBool(parameters,'FULL',context):
            return super().processAlgorithm(parameters,context,feedback)
        initial = dict(parameters)
        initial['OUTPUT'] = QgsProcessingUtils.generateTempFilename('building_points.gpkg')
        base_result = super().processAlgorithm(initial,context,feedback)
        detail = BuildingDetailsAlgorithm()
        detail.initAlgorithm()
        feedback.pushInfo('定位點下載完成，開始逐建號查完整資料；office 欄由地段清單自動取得。')
        return detail.processAlgorithm({
            'INPUT':base_result['OUTPUT'],'SECTION':'section','NUMBER':'build_no',
            'OFFICE_FIELD':'office','OFFICE':'','CACHE':parameters['CACHE'],
            'OUTPUT':parameters['OUTPUT'],
            'TABLE':parameters.get('TABLE') or QgsProcessing.TEMPORARY_OUTPUT,
        },context,feedback)
