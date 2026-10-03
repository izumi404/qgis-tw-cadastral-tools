from pathlib import Path
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QIcon, QDesktopServices
from qgis.PyQt.QtWidgets import QAction
from qgis.core import (QgsApplication, QgsProject, QgsProcessingProvider,
                       QgsProcessingFeatureSourceDefinition, QgsVectorLayer, QgsWkbTypes)

from .area_algorithm import AreaBuildingAlgorithm
from .detail_algorithm import BuildingDetailsAlgorithm


class BuildingProvider(QgsProcessingProvider):
    def id(self): return 'khbuildings'
    def name(self): return '高雄建物資料助手'
    def longName(self): return self.name()
    def icon(self): return QIcon(str(Path(__file__).with_name('icon.svg')))
    def loadAlgorithms(self):
        self.addAlgorithm(AreaBuildingAlgorithm())
        self.addAlgorithm(BuildingDetailsAlgorithm())


class BuildingPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.actions = []

    def initProcessing(self):
        if self.provider is None:
            self.provider = BuildingProvider()
            if not QgsApplication.processingRegistry().addProvider(self.provider):
                self.provider = None
                raise RuntimeError('無法註冊高雄建物資料工具，請檢查是否重複載入。')

    def initGui(self):
        self.initProcessing()
        if self.actions:
            return
        icon = QIcon(str(Path(__file__).with_name('icon.svg')))
        for label, callback, toolbar in [
            ('1. 依選取範圍取得建物資料', self.open_range, True),
            ('2. 補齊建物資料與樓層面積', self.open_details, True),
            ('使用說明', self.help, False),
        ]:
            action = QAction(icon, label, self.iface.mainWindow())
            action.triggered.connect(callback)
            self.iface.addPluginToMenu('高雄建物資料助手', action)
            if toolbar:
                self.iface.addToolBarIcon(action)
            self.actions.append((action, toolbar))

    def open_range(self):
        import processing
        params = {}
        active = self.iface.activeLayer()
        if not isinstance(active, QgsVectorLayer) or active.geometryType() != QgsWkbTypes.PolygonGeometry:
            matches = QgsProject.instance().mapLayersByName('計畫範圍')
            active = matches[0] if len(matches) == 1 else None
        if isinstance(active, QgsVectorLayer) and active.geometryType() == QgsWkbTypes.PolygonGeometry:
            params['RANGE'] = QgsProcessingFeatureSourceDefinition(active.id(), selectedFeaturesOnly=True) if active.selectedFeatureCount() else active
        processing.execAlgorithmDialog('khbuildings:polygon_buildings', params)

    def open_details(self):
        import processing
        active = self.iface.activeLayer()
        params = {}
        if isinstance(active, QgsVectorLayer) and active.geometryType() == QgsWkbTypes.PointGeometry:
            params['INPUT'] = QgsProcessingFeatureSourceDefinition(active.id(), selectedFeaturesOnly=True) if active.selectedFeatureCount() else active
        processing.execAlgorithmDialog('khbuildings:building_details', params)

    def help(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(__file__).with_name('README.html'))))

    def unload(self):
        for action, toolbar in self.actions:
            self.iface.removePluginMenu('高雄建物資料助手', action)
            if toolbar:
                self.iface.removeToolBarIcon(action)
            action.deleteLater()
        self.actions = []
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
