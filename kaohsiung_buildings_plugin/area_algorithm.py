from qgis.core import (QgsProcessing, QgsProcessingParameterBoolean,
                       QgsProcessingParameterFeatureSink, QgsProcessingUtils)
from .range_algorithm import BuildingDatesAlgorithm
from .detail_algorithm import BuildingDetailsAlgorithm


class AreaBuildingAlgorithm(BuildingDatesAlgorithm):
    def name(self): return 'polygon_buildings'
    def displayName(self): return '依選取範圍取得建物資料（自動辨認地段與事務所）'
    def createInstance(self): return AreaBuildingAlgorithm()

    def shortHelpString(self):
        return ('先選 polygon 再開工具，會自動啟用只處理已選取圖徵。亦可在範圍欄自行選圖層並勾「只處理已選取圖徵」。'
                '自動查相交行政區、地段及事務所代碼，再按建物定位點是否在多邊形內篩選。只支援高雄。'
                '預設下載日期、地址、樓層數、構造、面積及座標；勾「完整資料」會再逐建號取得內政部基本資料和各層／附屬面積。'
                '完整資料每建號需一次請求，26,896筆單計1秒間隔約7.5小時，另加網站回應時間。'
                '建議先用小範圍測試。快取可續跑，更新資料請用新空資料夾。定位點不是建物輪廓，完整性未經官方總數核證。')

    def initAlgorithm(self, config=None):
        super().initAlgorithm(config)
        self.addParameter(QgsProcessingParameterBoolean('FULL', '完整資料：另取主要用途及每層／附屬面積（較慢）', defaultValue=False))
        self.addParameter(QgsProcessingParameterFeatureSink('TABLE', '樓層面積表（勾完整資料時產生）',
                                                           QgsProcessing.TypeVector, optional=True))

    def processAlgorithm(self, parameters, context, feedback):
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
