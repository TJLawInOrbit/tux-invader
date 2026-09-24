#!/usr/bin/env bash
# Build The Tux InVader as an AppImage: dist/The_Tux_InVader-<version>-x86_64.AppImage
#
# Bundles a portable Python 3.12 (python-appimage, manylinux_2_28), PyQt6 and evdev with the app, so
# it runs on distros with glibc 2.34 or newer (PyQt6's Qt needs that). Every download is pinned and
# checked by SHA-256, and kept in build/downloads for the next build.
set -euo pipefail

PROJECT="$(dirname "$(dirname "$(realpath "${BASH_SOURCE[0]}")")")"
BUILD="$PROJECT/build"
APPDIR="$BUILD/AppDir"
DOWNLOADS="$BUILD/downloads"
VERSION="$(cd "$PROJECT" && python3 -c 'import vader5; print(vader5.VERSION)')"
OUTPUT="$PROJECT/dist/The_Tux_InVader-$VERSION-x86_64.AppImage"

PYTHON_FILE=python3.12-manylinux_2_28.AppImage
PYTHON_URL="https://github.com/niess/python-appimage/releases/download/python3.12/python3.12.14-cp312-cp312-manylinux_2_28_x86_64.AppImage"
PYTHON_SHA256=cefdd1b6e08dfb6c977d4233a4177ed3ee55a526991a122c8d92263f5901544f
TOOL_FILE=appimagetool-1.9.1.AppImage
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
TOOL_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
RUNTIME_FILE=runtime-x86_64-20251108
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA256=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d
PACKAGES=(PyQt6==6.11.0 PyQt6-Qt6==6.11.2 PyQt6-sip==13.12.0 evdev-binary==2.0.0)

download() {  # file, url, sha256
    local path="$DOWNLOADS/$1"
    if [ ! -f "$path" ]; then
        echo "Downloading $1"
        curl -fL --retry 3 -o "$path.part" "$2"
        mv "$path.part" "$path"
    fi
    if ! echo "$3  $path" | sha256sum --check --quiet; then
        echo "Checksum mismatch for $path (delete it to download again)" >&2
        exit 1
    fi
    chmod +x "$path"
}

mkdir -p "$DOWNLOADS" "$(dirname "$OUTPUT")"
download "$PYTHON_FILE" "$PYTHON_URL" "$PYTHON_SHA256"
download "$TOOL_FILE" "$TOOL_URL" "$TOOL_SHA256"
download "$RUNTIME_FILE" "$RUNTIME_URL" "$RUNTIME_SHA256"

echo "Unpacking Python"
rm -rf "$APPDIR" "$BUILD/squashfs-root"
(cd "$BUILD" && "$DOWNLOADS/$PYTHON_FILE" --appimage-extract >/dev/null)
mv "$BUILD/squashfs-root" "$APPDIR"
rm -rf "$APPDIR/AppRun" "$APPDIR/.DirIcon" "$APPDIR"/python*.desktop "$APPDIR/python.png" \
       "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons" "$APPDIR/usr/share/metainfo"
PYTHON="$APPDIR/opt/python3.12/bin/python3.12"

echo "Installing ${PACKAGES[*]}"
"$PYTHON" -I -m pip install --quiet --no-cache-dir --only-binary=:all: --disable-pip-version-check "${PACKAGES[@]}"
SITE="$("$PYTHON" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"

echo "Removing the parts of Qt and Python the app doesn't use"
"$PYTHON" -I - "$SITE/PyQt6" <<'PY'
import os, re, shutil, subprocess, sys

pyqt = sys.argv[1]
qt = os.path.join(pyqt, "Qt6")
libs = os.path.join(qt, "lib")
KEEP_MODULES = {"QtCore", "QtGui", "QtWidgets", "QtSvg", "QtDBus"}
KEEP_PLUGINS = {"platforms", "platformthemes", "platforminputcontexts", "iconengines", "imageformats",
                "wayland-decoration-client", "wayland-graphics-integration-client", "wayland-shell-integration",
                "xcbglintegrations"}

for name in os.listdir(pyqt):
    if name.endswith(".abi3.so") and name.split(".")[0] not in KEEP_MODULES:
        os.remove(os.path.join(pyqt, name))
# single plugins nobody needs in a desktop app; the VNC and PDF ones would also pull in Qt's network
# library, which needs Kerberos (libgssapi_krb5) that not every system has
DROP_PLUGIN_FILES = {"libqvnc.so", "libqeglfs.so", "libqlinuxfb.so", "libqminimalegl.so", "libqvkkhrdisplay.so",
                     "libqpdf.so", "libqtiff.so", "libqwebp.so", "libqtga.so", "libqwbmp.so", "libqicns.so"}
for name in os.listdir(os.path.join(qt, "plugins")):
    if name not in KEEP_PLUGINS:
        shutil.rmtree(os.path.join(qt, "plugins", name))
for folder, _, names in os.walk(os.path.join(qt, "plugins")):
    for name in names:
        if name in DROP_PLUGIN_FILES:
            os.remove(os.path.join(folder, name))
for name in ("qml", "translations", "resources"):
    shutil.rmtree(os.path.join(qt, name), ignore_errors=True)


def needed(path):
    output = subprocess.run(["objdump", "-p", path], capture_output=True, text=True, check=True).stdout
    return set(re.findall(r"NEEDED\s+(\S+)", output))


# keep only the Qt libraries that the remaining modules and plugins load, directly or through each other
roots = [os.path.join(pyqt, name) for name in os.listdir(pyqt) if name.endswith(".so")]
roots += [os.path.join(folder, name) for folder, _, names in os.walk(os.path.join(qt, "plugins"))
          for name in names if name.endswith(".so")]
keep, pending = set(), set().union(*(needed(root) for root in roots))
while pending:
    name = pending.pop()
    path = os.path.join(libs, name)
    if name in keep or not os.path.exists(path):
        continue
    keep |= {name, os.path.basename(os.path.realpath(path))}
    pending |= needed(path) - keep
for name in os.listdir(libs):
    if ".so" in name and name not in keep:
        os.remove(os.path.join(libs, name))
PY
STDLIB="$("$PYTHON" -I -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')"
rm -rf "$STDLIB/tkinter" "$STDLIB/idlelib" "$STDLIB/turtledemo" "$STDLIB/test" "$STDLIB"/lib-dynload/_tkinter* \
       "$APPDIR/usr/share/tcltk" "$APPDIR/opt/_internal/tcltk"

echo "Adding the app"
cp -r "$PROJECT/vader5" "$SITE/vader5"
find "$SITE/vader5" -name __pycache__ -prune -exec rm -rf {} +
"$PYTHON" -I -m compileall -q "$SITE/vader5" >/dev/null  # the AppImage is read-only, so compile now
"$PYTHON" -I -m pip uninstall --quiet --yes pip
rm -f "$APPDIR"/usr/bin/pip*

install -m 755 "$PROJECT/packaging/AppRun" "$APPDIR/AppRun"
sed "s/@VERSION@/$VERSION/" "$PROJECT/packaging/tux-invader.desktop" > "$APPDIR/tux-invader.desktop"
install -m 644 "$PROJECT/vader5/data/tux-invader.svg" "$APPDIR/tux-invader.svg"
install -Dm 644 "$PROJECT/LICENSE" "$APPDIR/usr/share/licenses/tux-invader/LICENSE"
if command -v rsvg-convert >/dev/null; then
    rsvg-convert -w 256 -h 256 "$APPDIR/tux-invader.svg" -o "$APPDIR/.DirIcon"
else
    ln -s tux-invader.svg "$APPDIR/.DirIcon"
fi

echo "Packing"
rm -f "$OUTPUT"
ARCH=x86_64 "$DOWNLOADS/$TOOL_FILE" --appimage-extract-and-run --no-appstream \
    --runtime-file "$DOWNLOADS/$RUNTIME_FILE" "$APPDIR" "$OUTPUT"
echo "Built $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
