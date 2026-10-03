"""Build a QGIS-installable archive without bundling local data or caches."""
import configparser
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "kaohsiung_buildings_plugin"
FILES = (
    "__init__.py", "plugin.py", "area_algorithm.py", "range_algorithm.py",
    "detail_algorithm.py", "parcel_algorithm.py", "metadata.txt", "icon.svg", "README.html",
)


def main():
    metadata = configparser.ConfigParser()
    metadata.read(PLUGIN / "metadata.txt", encoding="utf-8")
    version = metadata["general"]["version"]
    if not version or any(c not in "0123456789.-abcdefghijklmnopqrstuvwxyz" for c in version):
        raise ValueError("Unsafe or invalid version")
    for name in FILES:
        path = PLUGIN / name
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.suffix == ".py":
            compile(path.read_bytes(), str(path), "exec")
    output = ROOT / "dist" / f"{PLUGIN.name}-{version}.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(PLUGIN / name, f"{PLUGIN.name}/{name}")
    with ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("ZIP integrity check failed")
    print(output)


if __name__ == "__main__":
    main()
