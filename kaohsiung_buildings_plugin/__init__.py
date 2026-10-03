def classFactory(iface):
    from .plugin import BuildingPlugin
    return BuildingPlugin(iface)
